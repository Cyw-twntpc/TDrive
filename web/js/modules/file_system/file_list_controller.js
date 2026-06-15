const FileListHandler = {

    init(onSort, onUpdateSelection) {
        FileListHandler.setupSortableHeaders(onSort);
        // Integrate floating toolbar update into the selection callback
        const wrappedOnUpdateSelection = (AppState) => {
            FileListView.updateToolbarState(AppState);
            if (onUpdateSelection) onUpdateSelection(AppState);
        };
        FileListHandler.setupSelection(document.getElementById('file-list-container'), wrappedOnUpdateSelection);
        FileListHandler.setupFloatingToolbar();
    },

    setupFloatingToolbar() {
        if (!FileListView.floatingToolbar) return;

        // Bind Actions
        FileListView.ftDownloadBtn.addEventListener('click', () => ActionController.handleDownload());
        FileListView.ftMoveBtn.addEventListener('click', () => ActionController.handleMove());
        FileListView.ftRenameBtn.addEventListener('click', () => {
            if (AppState.selectedItems.length === 1) ActionController.handleRename(AppState.selectedItems[0]);
        });
        FileListView.ftShareBtn.addEventListener('click', () => {
            UIModals.showAlert(window.t('menu.share'), window.t('dialog.feature_soon'));
        });
        // Details button placeholder
        FileListView.ftDetailsBtn.addEventListener('click', () => {
            ActionController.handleDetails();
        });
        FileListView.ftTrashBtn.addEventListener('click', () => ActionController.handleDelete());
    },

    sortAndRender(AppState) {
        const { key, order } = AppState.currentSort;
        const sorter = (a, b) => {
            // Folders are always sorted before files.
            const aIsFolder = a.type === 'folder';
            const bIsFolder = b.type === 'folder';
            if (aIsFolder && !bIsFolder) return -1;
            if (!aIsFolder && bIsFolder) return 1;

            let valA, valB;
            switch (key) {
                case 'name':
                    // Use localeCompare for natural string sorting.
                    // 'zh-Hans-CN-u-co-pinyin' is for Chinese pinyin order, but works for English too.
                    return a.name.localeCompare(b.name, 'zh-Hans-CN-u-co-pinyin', { numeric: true, sensitivity: 'base' }) * (order === 'asc' ? 1 : -1);
                case 'type':
                    valA = UIManager.getFileTypeDescription(a.name, a.type === 'folder');
                    valB = UIManager.getFileTypeDescription(b.name, b.type === 'folder');
                    return valA.localeCompare(valB) * (order === 'asc' ? 1 : -1);
                case 'date':
                    valA = new Date(a.modif_date);
                    valB = new Date(b.modif_date);
                    break;
                case 'size':
                    valA = a.raw_size;
                    valB = b.raw_size;
                    break;
                default: return 0;
            }
            if (valA < valB) return order === 'asc' ? -1 : 1;
            if (valA > valB) return order === 'asc' ? 1 : -1;
            return a.name.localeCompare(b.name); // Secondary sort by name
        };

        const sortedFolders = [...(AppState.currentFolderContents.folders || [])].sort(sorter);
        const sortedFiles = [...(AppState.currentFolderContents.files || [])].sort(sorter);
        FileListView._updateFileListDOM({ folders: sortedFolders, files: sortedFiles }, AppState);

        // Update the sort indicators in the table header.
        document.querySelectorAll('.file-list-header .sortable').forEach(th => {
            th.classList.remove('asc', 'desc');
            if (th.dataset.sort === key) th.classList.add(order);
        });
    },

    setupSortableHeaders(onSort) {
        document.querySelectorAll('.file-list-header .sortable').forEach(th => {
            th.addEventListener('click', () => {
                const sortKey = th.dataset.sort;
                if (AppState.currentSort.key === sortKey) {
                    AppState.currentSort.order = AppState.currentSort.order === 'asc' ? 'desc' : 'asc';
                } else {
                    AppState.currentSort.key = sortKey;
                    AppState.currentSort.order = 'asc';
                }
                onSort();
            });
        });
    },

    _setupDropTarget(element, targetId) {
        element.addEventListener('dragover', (e) => {
            if (!AppState.isDragging) return;

            const isValid = ActionController.isValidMove(AppState.draggedItems, targetId);

            if (!isValid) {
                e.dataTransfer.dropEffect = 'none';
                return;
            }

            e.preventDefault(); // Allow drop
            e.stopPropagation();
            e.dataTransfer.dropEffect = 'move';
            element.classList.add('drop-target');
        });

        element.addEventListener('dragleave', () => {
            element.classList.remove('drop-target');
        });

        element.addEventListener('drop', (e) => {
            e.preventDefault();
            e.stopPropagation();
            element.classList.remove('drop-target');
            if (AppState.isDragging) {
                ActionController.executeMove(AppState.draggedItems, targetId);
            }
        });
    },

    _addSelectionListener(element, item, type, AppState) {
        element.addEventListener('click', (e) => {
            if (e.detail !== 1 || element.classList.contains('is-uploading')) return;

            const itemWithType = { ...item, type: type };
            const findIndex = () => AppState.selectedItems.findIndex(i => i.id === itemWithType.id && i.type === itemWithType.type);
            let itemIndex = findIndex();

            if (e.ctrlKey) { // Ctrl+click to toggle selection
                if (itemIndex > -1) {
                    element.classList.remove('selected');
                    AppState.selectedItems.splice(itemIndex, 1);
                } else {
                    element.classList.add('selected');
                    AppState.selectedItems.push(itemWithType);
                }
            } else { // Single click to select one item
                if (AppState.selectedItems.length === 1 && itemIndex === 0) return;

                document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0; 
                element.classList.add('selected');
                AppState.selectedItems.push(itemWithType);
            }
            FileListView.updateToolbarState(AppState);
        });
    },

    setupSelection(containerEl, onUpdate) {
        let isDragging = false;
        let startX = 0, startY = 0;
        let autoScrollFrameId = null;
        let lastClientX = 0, lastClientY = 0;

        const updateSelectionBox = (clientX, clientY) => {
            const rect = containerEl.getBoundingClientRect();
            const headerEl = containerEl.querySelector('.file-list-header');
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

            Object.assign(FileListView.selectionBox.style, { 
                left: `${newLeft}px`, 
                top: `${newTop}px`, 
                width: `${newWidth}px`, 
                height: `${newHeight}px` 
            });

            const boxRect = FileListView.selectionBox.getBoundingClientRect();
            document.querySelectorAll('.file-item:not(.is-uploading)').forEach(itemEl => {
                const itemRect = itemEl.getBoundingClientRect();
                const intersects = !(boxRect.right < itemRect.left || boxRect.left > itemRect.right || boxRect.bottom < itemRect.top || boxRect.top > itemRect.bottom);
                
                const itemId = parseFloat(itemEl.dataset.id);
                const itemType = itemEl.dataset.type;
                const isSelected = AppState.selectedItems.some(i => i.id === itemId && i.type === itemType);

                if (intersects) {
                    if (!isSelected) {
                        itemEl.classList.add('selected');
                        const itemData = (itemType === 'folder')
                            ? AppState.currentFolderContents.folders.find(i => i.id === itemId)
                            : AppState.currentFolderContents.files.find(i => i.id === itemId);
                        if (itemData) AppState.selectedItems.push({ ...itemData, type: itemType });
                    }
                } else {
                    if (!window.event?.ctrlKey && isSelected) {
                        itemEl.classList.remove('selected');
                        const indexToRemove = AppState.selectedItems.findIndex(i => i.id === itemId && i.type === itemType);
                        if (indexToRemove > -1) AppState.selectedItems.splice(indexToRemove, 1);
                    }
                }
            });
            if (onUpdate) onUpdate(AppState);
        };

        const autoScrollLoop = () => {
            if (!isDragging) return;
            const rect = containerEl.getBoundingClientRect();
            const headerEl = containerEl.querySelector('.file-list-header');
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
            const clickedItem = e.target.closest('.file-item');
            if (clickedItem && clickedItem.classList.contains('selected')) return;
            if (e.target.closest('button') || e.target.closest('a')) return;

            const rect = containerEl.getBoundingClientRect();
            const headerEl = containerEl.querySelector('.file-list-header');
            const headerHeight = headerEl ? headerEl.offsetHeight : 0;
            if (e.clientY < rect.top + headerHeight) return;

            containerEl.classList.add('is-selecting');
            isDragging = false; 
            startX = e.clientX - rect.left; 
            startY = e.clientY - rect.top + containerEl.scrollTop;
            
            Object.assign(FileListView.selectionBox.style, { left: `${startX}px`, top: `${startY}px`, width: '0px', height: '0px', display: 'block' });

            if (!clickedItem && !e.ctrlKey) {
                 document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
                 AppState.selectedItems.length = 0;
                 if (onUpdate) onUpdate(AppState);
            }

            const onMouseMove = (moveE) => {
                lastClientX = moveE.clientX;
                lastClientY = moveE.clientY;

                if (!isDragging) {
                     if (Math.abs(moveE.clientX - e.clientX) < 5 && Math.abs(moveE.clientY - e.clientY) < 5) return;
                     isDragging = true;
                     moveE.preventDefault();
                     if (!e.ctrlKey) {
                         document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
                         AppState.selectedItems.length = 0;
                     }
                     if (!autoScrollFrameId) autoScrollLoop();
                }
                updateSelectionBox(moveE.clientX, moveE.clientY);
            };

            const onMouseUp = () => {
                containerEl.classList.remove('is-selecting');
                isDragging = false; 
                FileListView.selectionBox.style.display = 'none';
                if (autoScrollFrameId) {
                    cancelAnimationFrame(autoScrollFrameId);
                    autoScrollFrameId = null;
                }
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                if (onUpdate) onUpdate(AppState);
            };
            
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    },

    async loadThumbnails(folderId) {
        if (!folderId) return;
        try {
            const result = await ApiService.getThumbnails(folderId);
            
            if (result && result.success && result.thumbnails) {
                AppState.currentThumbnails = result.thumbnails; // Cache for Gallery
                const entries = Object.entries(result.thumbnails);
                
                // Batch process DOM updates using requestAnimationFrame to avoid freezing UI
                const BATCH_SIZE = 20;
                let currentIndex = 0;

                const processBatch = () => {
                    const endIndex = Math.min(currentIndex + BATCH_SIZE, entries.length);
                    for (let i = currentIndex; i < endIndex; i++) {
                        const [fileId, b64] = entries[i];
                        const src = `data:image/jpeg;base64,${b64}`;
                        
                        // Update Grid View
                        const gridItem = FileListView.fileListBodyEl.querySelector(`.file-item[data-id="${fileId}"][data-type="file"]`);
                        const gridImg = gridItem ? gridItem.querySelector('.grid-thumb-img') : null;
                        
                        if (gridImg) {
                            gridImg.src = src;
                            gridImg.classList.remove('hidden');
                            const gridIcon = gridItem.querySelector('.grid-thumb-icon');
                            if (gridIcon) gridIcon.classList.add('hidden');
                        }

                        // Update List View
                        const listItem = FileListView.fileListBodyEl.querySelector(`.file-item[data-id="${fileId}"][data-type="file"]`);
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
            console.error("Failed to load thumbnails:", e);
        }
    },

};
