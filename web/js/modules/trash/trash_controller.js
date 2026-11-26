const TrashHandler = {

    init() {
        TrashHandler.setupSortHeaders();
        TrashHandler.setupEmptyButton();
        TrashHandler.setupSelection();
        TrashHandler.setupBulkActions();
    },

    /** Restore a single trashed item and reload the trash list. */
    async restoreItem(item) {
        UIManager.startProgress();
        UIManager.setInteractionLock(true);
        try {
            const result = await ApiService.restoreItems([{ id: item.id, type: item.type }]);
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
    },

    /** Permanently delete a single trashed item after user confirmation. */
    async deleteItemPermanently(item) {
        const confirmed = await UIModals.showConfirm(
            window.t('trash.btn_delete'),
            window.t('trash.confirm_delete_single').replace('{name}', item.name),
            'btn-danger'
        );

        if (!confirmed) return;

        UIManager.startProgress();
        UIManager.setInteractionLock(true);
        try {
            const result = await ApiService.deleteItemsPermanently([{ id: item.id, type: item.type }]);
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
        SelectionManager.bind({
            containerEl: TrashView.section,
            getSelectionBox: () => TrashView.selectionBox,
            headerSelector: '.trash-list-header',
            itemSelector: '.trash-item',
            selectableSelector: '.trash-item',
            ignoreSelector: '.trash-action-btn',
            getScopeRoot: () => TrashView.listBody,
            buildItemLookup: () => {
                const trashLookup = new Map();
                (AppState.trashItems || []).forEach(i => trashLookup.set(`${i.type}:${i.id}`, i));
                return trashLookup;
            },
            toSelectedEntry: (itemData) => itemData,
            onUpdate: () => TrashView.updateBulkActionState(),
        });
    },

};
