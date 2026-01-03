/**
 * @fileoverview Manages the rendering and interactions for the Recycle Bin page.
 */
const TrashHandler = {
    // --- DOM Elements ---
    listBody: document.getElementById('trash-list-body'),
    emptyBtn: document.getElementById('empty-trash-btn'),
    selectionBox: document.getElementById('trash-selection-box'),
    section: document.querySelector('#page-trash .file-list-section'),
    bulkActions: document.getElementById('trash-bulk-actions'),
    restoreBtn: document.getElementById('trash-restore-btn'),
    deleteBtn: document.getElementById('trash-delete-btn'),

    init() {
        this.setupSortHeaders();
        this.setupEmptyButton();
        this.setupSelection();
        this.setupBulkActions();
    },

    updateBulkActionState() {
        if (AppState.selectedItems.length > 0) {
            this.bulkActions.classList.remove('hidden');
        } else {
            this.bulkActions.classList.add('hidden');
        }
    },

    setupBulkActions() {
        this.restoreBtn.addEventListener('click', async () => {
            const count = AppState.selectedItems.length;
            if (count === 0) return;

            UIManager.startProgress();
            UIManager.setInteractionLock(true);
            try {
                // Clone the array because loadTrashItems will clear selectedItems
                const itemsToRestore = [...AppState.selectedItems];
                const result = await ApiService.restoreItems(itemsToRestore.map(i => ({ id: i.id, type: i.type })));
                if (result.success) {
                    this.loadTrashItems();
                } else {
                    UIManager.handleBackendError(result);
                }
            } catch (e) {
                console.error(e);
                UIManager.handleBackendError({ message: "還原失敗" });
            } finally {
                UIManager.stopProgress();
                UIManager.setInteractionLock(false);
            }
        });

        this.deleteBtn.addEventListener('click', async () => {
            const count = AppState.selectedItems.length;
            if (count === 0) return;

            const confirmed = await UIModals.showConfirm(
                '永久刪除',
                `確定要永久刪除選取的 ${count} 個項目嗎？`,
                'btn-danger'
            );
            
            if (!confirmed) return;

            UIManager.startProgress();
            UIManager.setInteractionLock(true);
            try {
                const itemsToDelete = [...AppState.selectedItems];
                const result = await ApiService.deleteItemsPermanently(itemsToDelete.map(i => ({ id: i.id, type: i.type })));
                if (result.success) {
                    this.loadTrashItems();
                } else {
                    UIManager.handleBackendError(result);
                }
            } catch (e) {
                console.error(e);
                UIManager.handleBackendError({ message: "刪除失敗" });
            } finally {
                UIManager.stopProgress();
                UIManager.setInteractionLock(false);
            }
        });
    },

    /**
     * Loads trash items from the backend, pre-calculates paths, and renders them.
     */
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
                    item.displayPath = this._getOriginalPath(item.original_parent_id);
                });

                AppState.trashItems = allItems;
                this.sortAndRender();
            } else {
                console.error("Invalid trash response:", response);
                UIManager.handleBackendError(response || { message: "無法載入回收桶內容。" });
            }
        } catch (e) {
            console.error("Error loading trash:", e);
            UIManager.handleBackendError({ message: "系統錯誤，請重試。" });
        } finally {
            UIManager.stopProgress();
        }
    },

    /**
     * Sorts the cached trash items and updates the DOM.
     */
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

        this.render();
    },

    render() {
        this.listBody.innerHTML = '';
        AppState.selectedItems.length = 0; // Reset selection on render
        this.updateBulkActionState();
        
        if (AppState.trashItems.length === 0) {
            this.listBody.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-trash-alt"></i>
                    <p>回收桶是空的</p>
                </div>`;
            this.emptyBtn.disabled = true;
            return;
        }
        
        this.emptyBtn.disabled = false;

        const fragment = document.createDocumentFragment();
        
        AppState.trashItems.forEach(item => {
            const el = document.createElement('div');
            el.className = 'trash-item';
            el.dataset.id = item.id;
            el.dataset.type = item.type;
            
            const iconClass = item.type === 'folder' ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
            
            const pathName = item.displayPath || '未知位置';
            
            el.innerHTML = `
                <div class="trash-col-name">
                    <div class="trash-col-main">
                        <i class="${iconClass}"></i>
                        <span title="${item.name}">${item.name}</span>
                    </div>
                    <div class="trash-col-actions">
                        <button class="trash-action-btn restore-btn" title="還原"><i class="fas fa-undo-alt"></i></button>
                        <button class="trash-action-btn delete-btn" title="永久刪除"><i class="fas fa-trash-alt"></i></button>
                    </div>
                </div>
                <div>${item.trashed_date}</div>
                <div>${item.type === 'folder' ? '資料夾' : '檔案'}</div>
                <div class="trash-col-path" title="${pathName}">${pathName}</div> 
                <div>${item.size}</div>
            `;
            
            // Bind actions
            el.querySelector('.restore-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                this.restoreItem(item);
            });
            el.querySelector('.delete-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                this.deleteItemPermanently(item);
            });

            this._addSelectionListener(el, item);
            fragment.appendChild(el);
        });
        
        this.listBody.appendChild(fragment);
    },

    /**
     * Recursively builds the full path string from a folder ID.
     */
    _getOriginalPath(folderId) {
        if (!folderId) return '路徑不存在';
        if (!AppState || !AppState.folderMap) return '路徑不存在';
        
        const path = [];
        let current = AppState.folderMap.get(folderId);

        // Case 1: ID not found in current map
        if (!current) return '路徑不存在';

        while (current) {
            path.unshift(current.name);
            
            // Reached Root
            if (current.parent_id === null) {
                return path.join(' / ');
            }

            // Move up
            const next = AppState.folderMap.get(current.parent_id);
            
            // Case 2: Broken chain
            if (!next) return '路徑不存在';
            
            current = next;
        }

        return path.join(' / ');
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
                
                this.sortAndRender();
            });
        });
    },

    setupEmptyButton() {
        this.emptyBtn.addEventListener('click', async () => {
            const confirmed = await UIModals.showConfirm(
                '清空回收桶', 
                '確定要永久刪除回收桶中的所有項目嗎？<b>此動作無法復原。</b>',
                'btn-danger'
            );
            
            if (confirmed) {
                UIManager.startProgress();
                UIManager.setInteractionLock(true);
                try {
                    const result = await ApiService.emptyTrash();
                    if (result.success) {
                        this.loadTrashItems(); // Reload
                    } else {
                        UIManager.handleBackendError(result);
                    }
                } catch (e) {
                    console.error(e);
                    UIManager.handleBackendError({ message: "清空失敗" });
                } finally {
                    UIManager.stopProgress();
                    UIManager.setInteractionLock(false);
                }
            }
        });
    },

    async restoreItem(item) {
        UIManager.startProgress();
        UIManager.setInteractionLock(true);
        try {
            const result = await ApiService.restoreItems([{ id: item.id, type: item.type }]);
            if (result.success) {
                this.loadTrashItems();
            } else {
                UIManager.handleBackendError(result);
            }
        } catch (e) {
            console.error(e);
            UIManager.handleBackendError({ message: "還原失敗" });
        } finally {
            UIManager.stopProgress();
            UIManager.setInteractionLock(false);
        }
    },

    async deleteItemPermanently(item) {
        const confirmed = await UIModals.showConfirm(
            '永久刪除',
            `確定要永久刪除 "${item.name}" 嗎？`,
            'btn-danger'
        );
        
        if (!confirmed) return;

        UIManager.startProgress();
        UIManager.setInteractionLock(true);
        try {
            const result = await ApiService.deleteItemsPermanently([{ id: item.id, type: item.type }]);
            if (result.success) {
                this.loadTrashItems();
            } else {
                UIManager.handleBackendError(result);
            }
        } catch (e) {
            console.error(e);
            UIManager.handleBackendError({ message: "刪除失敗" });
        } finally {
            UIManager.stopProgress();
            UIManager.setInteractionLock(false);
        }
    },

    // --- Selection Logic (Drag-to-Select & Click) ---

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

                this.listBody.querySelectorAll('.trash-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0; 
                element.classList.add('selected');
                AppState.selectedItems.push(item);
            }
            this.updateBulkActionState();
        });
    },

    setupSelection() {
        const containerEl = this.section;
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

            Object.assign(this.selectionBox.style, { 
                left: `${newLeft}px`, 
                top: `${newTop}px`, 
                width: `${newWidth}px`, 
                height: `${newHeight}px` 
            });

            const boxRect = this.selectionBox.getBoundingClientRect();
            this.listBody.querySelectorAll('.trash-item').forEach(itemEl => {
                const itemRect = itemEl.getBoundingClientRect();
                const intersects = !(boxRect.right < itemRect.left || boxRect.left > itemRect.right || boxRect.bottom < itemRect.top || boxRect.top > itemRect.bottom);
                
                const itemId = parseFloat(itemEl.dataset.id);
                const itemType = itemEl.dataset.type;
                const isSelected = AppState.selectedItems.some(i => i.id === itemId && i.type === itemType);

                if (intersects) {
                    if (!isSelected) {
                        itemEl.classList.add('selected');
                        const itemData = AppState.trashItems.find(i => i.id === itemId && i.type === itemType);
                        if (itemData) AppState.selectedItems.push(itemData);
                    }
                } else {
                    if (!window.event?.ctrlKey && isSelected) {
                        itemEl.classList.remove('selected');
                        const idx = AppState.selectedItems.findIndex(i => i.id === itemId && i.type === itemType);
                        if (idx > -1) AppState.selectedItems.splice(idx, 1);
                    }
                }
            });
            this.updateBulkActionState();
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
            
            Object.assign(this.selectionBox.style, { left: `${startX}px`, top: `${startY}px`, width: '0px', height: '0px', display: 'block' });

            if (!clickedItem && !e.ctrlKey) {
                this.listBody.querySelectorAll('.trash-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0;
                this.updateBulkActionState();
            }

            const onMouseMove = (moveE) => {
                lastClientX = moveE.clientX;
                lastClientY = moveE.clientY;

                if (!isDragging) {
                     if (Math.abs(moveE.clientX - e.clientX) < 5 && Math.abs(moveE.clientY - e.clientY) < 5) return;
                     isDragging = true;
                     moveE.preventDefault();
                     if (!e.ctrlKey) {
                         this.listBody.querySelectorAll('.trash-item.selected').forEach(el => el.classList.remove('selected'));
                         AppState.selectedItems.length = 0;
                     }
                     if (!autoScrollFrameId) autoScrollLoop();
                }
                updateSelectionBox(moveE.clientX, moveE.clientY);
            };

            const onMouseUp = () => {
                containerEl.classList.remove('is-selecting');
                isDragging = false; 
                this.selectionBox.style.display = 'none';
                if (autoScrollFrameId) {
                    cancelAnimationFrame(autoScrollFrameId);
                    autoScrollFrameId = null;
                }
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                this.updateBulkActionState();
            };
            
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    }
};
