const TrashHandler = {

    init() {
        TrashHandler.setupSortHeaders();
        TrashHandler.setupEmptyButton();
        TrashHandler.setupSelection();
        TrashHandler.setupBulkActions();
    },

    setupBulkActions() {
        TrashView.restoreBtn.addEventListener('click', async () => {
            const count = AppState.selectedItems.length;
            if (count === 0) return;

            UIManager.startProgress();
            UIManager.setInteractionLock(true);
            try {
                // Clone the array because loadTrashItems will clear selectedItems
                const itemsToRestore = [...AppState.selectedItems];
                const result = await ApiService.restoreItems(itemsToRestore.map(i => ({ id: i.id, type: i.type })));
                if (result.success) {
                    TrashHandler.loadTrashItems();
                } else {
                    UIManager.handleBackendError(result);
                }
            } catch (e) {
                console.error(e);
                UIManager.handleBackendError({ message: window.t('dialog.restore_failed') });
            } finally {
                UIManager.stopProgress();
                UIManager.setInteractionLock(false);
            }
        });

        TrashView.deleteBtn.addEventListener('click', async () => {
            const count = AppState.selectedItems.length;
            if (count === 0) return;

            const confirmed = await UIModals.showConfirm(
                window.t('trash.btn_delete'),
                window.t('trash.confirm_delete_multi').replace('{count}', count),
                'btn-danger'
            );
            
            if (!confirmed) return;

            UIManager.startProgress();
            UIManager.setInteractionLock(true);
            try {
                const itemsToDelete = [...AppState.selectedItems];
                const result = await ApiService.deleteItemsPermanently(itemsToDelete.map(i => ({ id: i.id, type: i.type })));
                if (result.success) {
                    TrashHandler.loadTrashItems();
                } else {
                    UIManager.handleBackendError(result);
                }
            } catch (e) {
                console.error(e);
                UIManager.handleBackendError({ message: window.t('dialog.delete_failed') });
            } finally {
                UIManager.stopProgress();
                UIManager.setInteractionLock(false);
            }
        });
    },

    async loadTrashItems() {
        UIManager.startProgress();
        try {
            const response = await ApiService.getTrashItems();
            if (response && response.folders && response.files) {
                // Combine and normalize items
                const allItems = [
                    ...response.folders.map(f => ({ ...f, type: 'folder' })),
                    ...response.files.map(f => ({ ...f, type: 'file' }))
                ];
                
                // Pre-calculate display paths for efficient sorting/rendering
                allItems.forEach(item => {
                    item.displayPath = TrashView._getOriginalPath(item.original_parent_id);
                });

                AppState.trashItems = allItems;
                if (TrashView._thumbnails instanceof Map) {
                    TrashView._thumbnails.clear();
                } else {
                    TrashView._thumbnails = new Map();
                }
                TrashHandler.sortAndRender();

                if (response.files && response.files.length > 0 && response.recycle_bin_id) {
                    TrashHandler.loadThumbnails(response.recycle_bin_id);
                }
            } else {
                console.error("Invalid trash response:", response);
                UIManager.handleBackendError(response || { message: window.t('trash.empty_text') });
            }
        } catch (e) {
            console.error("Error loading trash:", e);
            UIManager.handleBackendError({ message: window.t('dialog.sys_err_retry') });
        } finally {
            UIManager.stopProgress();
        }
    },

    async loadThumbnails(recycleBinId) {
        if (!recycleBinId) return;
        try {
            const result = await ApiService.getThumbnails(recycleBinId);
            if (result && result.success && result.thumbnails) {
                const entries = Object.entries(result.thumbnails);
                if (!(TrashView._thumbnails instanceof Map)) {
                    TrashView._thumbnails = new Map();
                }
                entries.forEach(([fileId, b64]) => {
                    TrashView._thumbnails.set(String(fileId), b64);
                });

                // Index currently rendered file DOM nodes once to avoid O(T * R) selector scans
                const elementMap = new Map();
                TrashView.listBody.querySelectorAll('.trash-item[data-type="file"]').forEach(el => {
                    elementMap.set(String(el.dataset.id), el);
                });
                
                // Batch process DOM updates using requestAnimationFrame
                const BATCH_SIZE = 20;
                let currentIndex = 0;

                const processBatch = () => {
                    const endIndex = Math.min(currentIndex + BATCH_SIZE, entries.length);
                    for (let i = currentIndex; i < endIndex; i++) {
                        const [fileId, b64] = entries[i];
                        const src = `data:image/jpeg;base64,${b64}`;
                        
                        const listItem = elementMap.get(String(fileId));
                        const listImg = listItem ? listItem.querySelector('.list-thumb-img') : null;

                        if (listImg) {
                            listImg.src = src;
                            listImg.classList.remove('hidden');
                            const listIcon = listItem.querySelector('.list-thumb-icon');
                            if (listIcon) listIcon.classList.add('hidden');
                        }
                    }
                    currentIndex = endIndex;
                    if (currentIndex < entries.length) {
                        requestAnimationFrame(processBatch);
                    }
                };
                requestAnimationFrame(processBatch);
            }
        } catch (e) {
            console.error("Failed to load trash thumbnails:", e);
        }
    },

    sortAndRender() {
        const { key, order } = AppState.trashSort;
        
        AppState.trashItems.sort((a, b) => {
            let valA, valB;
            
            switch (key) {
                case 'name':
                    valA = a.name;
                    valB = b.name;
                    return valA.localeCompare(valB, 'zh-Hans-CN-u-co-pinyin') * (order === 'asc' ? 1 : -1);
                case 'size':
                    valA = a.raw_size || 0;
                    valB = b.raw_size || 0;
                    break;
                case 'trashed_date':
                    // Use raw timestamp for accurate sorting
                    valA = a.trashed_date_ts || 0;
                    valB = b.trashed_date_ts || 0;
                    break;
                case 'type':
                    valA = a.type;
                    valB = b.type;
                    break;
                case 'original_parent_id':
                    // Sort by the pre-calculated path string
                    valA = a.displayPath;
                    valB = b.displayPath;
                    return valA.localeCompare(valB, 'zh-Hans-CN-u-co-pinyin') * (order === 'asc' ? 1 : -1);
                default:
                    return 0;
            }
            
            if (valA < valB) return order === 'asc' ? -1 : 1;
            if (valA > valB) return order === 'asc' ? 1 : -1;
            return 0;
        });

        TrashView.render();
    },

    setupSortHeaders() {
        document.querySelectorAll('.trash-list-header .sortable').forEach(header => {
            header.addEventListener('click', () => {
                const key = header.dataset.sort;
                if (AppState.trashSort.key === key) {
                    AppState.trashSort.order = AppState.trashSort.order === 'asc' ? 'desc' : 'asc';
                } else {
                    AppState.trashSort.key = key;
                    AppState.trashSort.order = 'asc';
                }
                
                // Update UI indicators
                document.querySelectorAll('.trash-list-header .sortable').forEach(h => h.classList.remove('asc', 'desc'));
                header.classList.add(AppState.trashSort.order);
                
                TrashHandler.sortAndRender();
            });
        });
    },

    setupEmptyButton() {
        TrashView.emptyBtn.addEventListener('click', async () => {
            const confirmed = await UIModals.showConfirm(
                window.t('trash.confirm_empty_title'), 
                window.t('trash.confirm_empty_msg'),
                'btn-danger'
            );
            
            if (confirmed) {
                UIManager.startProgress();
                UIManager.setInteractionLock(true);
                try {
                    const result = await ApiService.emptyTrash();
                    if (result.success) {
                        TrashHandler.loadTrashItems(); // Reload
                    } else {
                        UIManager.handleBackendError(result);
                    }
                } catch (e) {
                    console.error(e);
                    UIManager.handleBackendError({ message: window.t('dialog.empty_failed') });
                } finally {
                    UIManager.stopProgress();
                    UIManager.setInteractionLock(false);
                }
            }
        });
    },

    _addSelectionListener(element, item) {
        element.addEventListener('click', (e) => {
            if (e.detail !== 1 || e.target.closest('.trash-action-btn')) return;

            const itemIndex = AppState.selectedItems.findIndex(i => i.id === item.id && i.type === item.type);

            if (e.ctrlKey) { // Ctrl+click: Toggle
                if (itemIndex > -1) {
                    element.classList.remove('selected');
                    AppState.selectedItems.splice(itemIndex, 1);
                } else {
                    element.classList.add('selected');
                    AppState.selectedItems.push(item);
                }
            } else { // Single click: Select only this
                if (AppState.selectedItems.length === 1 && itemIndex === 0) return; // Already selected solo

                TrashView.listBody.querySelectorAll('.trash-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0; 
                element.classList.add('selected');
                AppState.selectedItems.push(item);
            }
            TrashView.updateBulkActionState();
        });
    },

    setupSelection() {
        const containerEl = TrashView.section;
        let isDragging = false;
        let startX = 0, startY = 0;
        let autoScrollFrameId = null;
        let lastClientX = 0, lastClientY = 0;

        const updateSelectionBox = (clientX, clientY) => {
            const rect = containerEl.getBoundingClientRect();
            const headerEl = containerEl.querySelector('.trash-list-header');
            const headerHeight = headerEl ? headerEl.offsetHeight : 0;
            const viewTop = rect.top + headerHeight;
            
            const clampedX = Math.max(rect.left, Math.min(rect.right, clientX));
            const clampedY = Math.max(viewTop, Math.min(rect.bottom, clientY));

            const currentContentX = clampedX - rect.left;
            const currentContentY = clampedY - rect.top + containerEl.scrollTop;

            const newLeft = Math.min(startX, currentContentX);
            const newTop = Math.min(startY, currentContentY);
            const newWidth = Math.abs(startX - currentContentX);
            const newHeight = Math.abs(startY - currentContentY);

            Object.assign(TrashView.selectionBox.style, { 
                left: `${newLeft}px`, 
                top: `${newTop}px`, 
                width: `${newWidth}px`, 
                height: `${newHeight}px` 
            });

            const boxRect = TrashView.selectionBox.getBoundingClientRect();
            const trashLookup = new Map();
            (AppState.trashItems || []).forEach(i => trashLookup.set(`${i.type}:${i.id}`, i));
            const selectedKeySet = new Set(AppState.selectedItems.map(i => `${i.type}:${i.id}`));
            const ctrlHeld = Boolean(window.event?.ctrlKey);

            // Batch geometry reads before mutating classes to prevent layout thrashing
            const measurements = [];
            TrashView.listBody.querySelectorAll('.trash-item').forEach(itemEl => {
                const itemRect = itemEl.getBoundingClientRect();
                const intersects = !(boxRect.right < itemRect.left || boxRect.left > itemRect.right || boxRect.bottom < itemRect.top || boxRect.top > itemRect.bottom);
                measurements.push({
                    itemEl,
                    intersects,
                    itemId: parseFloat(itemEl.dataset.id),
                    itemType: itemEl.dataset.type,
                });
            });

            let removedAny = false;
            measurements.forEach(({ itemEl, intersects, itemId, itemType }) => {
                const key = `${itemType}:${itemId}`;
                const isSelected = selectedKeySet.has(key);

                if (intersects) {
                    if (!isSelected) {
                        itemEl.classList.add('selected');
                        const itemData = trashLookup.get(key);
                        if (itemData) {
                            AppState.selectedItems.push(itemData);
                            selectedKeySet.add(key);
                        }
                    }
                } else if (!ctrlHeld && isSelected) {
                    itemEl.classList.remove('selected');
                    selectedKeySet.delete(key);
                    removedAny = true;
                }
            });

            if (removedAny) {
                const retained = AppState.selectedItems.filter(i => selectedKeySet.has(`${i.type}:${i.id}`));
                AppState.selectedItems.splice(0, AppState.selectedItems.length, ...retained);
            }
            TrashView.updateBulkActionState();
        };

        const autoScrollLoop = () => {
            if (!isDragging) return;
            const rect = containerEl.getBoundingClientRect();
            const headerEl = containerEl.querySelector('.trash-list-header');
            const headerHeight = headerEl ? headerEl.offsetHeight : 0;
            const viewTop = rect.top + headerHeight;
            let scrolled = false;

            const BASE_SPEED = 2;
            const MAX_SPEED = 30;
            const SENSITIVITY = 0.4;

            if (lastClientY < viewTop) {
                if (containerEl.scrollTop > 0) {
                    const dist = viewTop - lastClientY;
                    const speed = Math.min(MAX_SPEED, BASE_SPEED + (dist * SENSITIVITY));
                    containerEl.scrollTop -= speed;
                    scrolled = true;
                }
            } else if (lastClientY > rect.bottom) {
                const maxScroll = containerEl.scrollHeight - containerEl.clientHeight;
                if (containerEl.scrollTop < maxScroll) {
                    const dist = lastClientY - rect.bottom;
                    const speed = Math.min(MAX_SPEED, BASE_SPEED + (dist * SENSITIVITY));
                    containerEl.scrollTop += speed;
                    scrolled = true;
                }
            }

            if (scrolled) updateSelectionBox(lastClientX, lastClientY);
            autoScrollFrameId = requestAnimationFrame(autoScrollLoop);
        };

        containerEl.addEventListener('mousedown', e => {
            const clickedItem = e.target.closest('.trash-item');
            if (clickedItem && clickedItem.classList.contains('selected')) return;
            if (e.target.closest('.trash-action-btn')) return;

            const rect = containerEl.getBoundingClientRect();
            const headerEl = containerEl.querySelector('.trash-list-header');
            const headerHeight = headerEl ? headerEl.offsetHeight : 0;
            if (e.clientY < rect.top + headerHeight) return;

            containerEl.classList.add('is-selecting');
            isDragging = false; 
            startX = e.clientX - rect.left; 
            startY = e.clientY - rect.top + containerEl.scrollTop;
            
            Object.assign(TrashView.selectionBox.style, { left: `${startX}px`, top: `${startY}px`, width: '0px', height: '0px', display: 'block' });

            if (!clickedItem && !e.ctrlKey) {
                TrashView.listBody.querySelectorAll('.trash-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0;
                TrashView.updateBulkActionState();
            }

            const onMouseMove = (moveE) => {
                lastClientX = moveE.clientX;
                lastClientY = moveE.clientY;

                if (!isDragging) {
                     if (Math.abs(moveE.clientX - e.clientX) < 5 && Math.abs(moveE.clientY - e.clientY) < 5) return;
                     isDragging = true;
                     moveE.preventDefault();
                     if (!e.ctrlKey) {
                         TrashView.listBody.querySelectorAll('.trash-item.selected').forEach(el => el.classList.remove('selected'));
                         AppState.selectedItems.length = 0;
                     }
                     if (!autoScrollFrameId) autoScrollLoop();
                }
                updateSelectionBox(moveE.clientX, moveE.clientY);
            };

            const onMouseUp = () => {
                containerEl.classList.remove('is-selecting');
                isDragging = false; 
                TrashView.selectionBox.style.display = 'none';
                if (autoScrollFrameId) {
                    cancelAnimationFrame(autoScrollFrameId);
                    autoScrollFrameId = null;
                }
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                TrashView.updateBulkActionState();
            };
            
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    },

};
