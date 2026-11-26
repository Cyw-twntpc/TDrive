const FileListHandler = {
    _thumbLoadGen: 0,

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
        SelectionManager.bind({
            containerEl,
            getSelectionBox: () => FileListView.selectionBox,
            headerSelector: '.file-list-header',
            itemSelector: '.file-item',
            selectableSelector: '.file-item:not(.is-uploading)',
            ignoreSelector: 'button, a',
            getScopeRoot: () => document,
            buildItemLookup: () => {
                const itemLookup = new Map();
                (AppState.currentFolderContents.folders || []).forEach(f => itemLookup.set(`folder:${f.id}`, f));
                (AppState.currentFolderContents.files || []).forEach(f => itemLookup.set(`file:${f.id}`, f));
                return itemLookup;
            },
            toSelectedEntry: (itemData, itemType) => ({ ...itemData, type: itemType }),
            onUpdate,
        });
    },

    /** Apply a base64 thumbnail string to either a grid or list item element. */
    _applyThumbnailToElement(itemEl, b64) {
        if (!itemEl || !b64) return;
        const src = `data:image/jpeg;base64,${b64}`;

        const gridImg = itemEl.querySelector('.grid-thumb-img');
        if (gridImg) {
            gridImg.src = src;
            gridImg.classList.remove('hidden');
            const gridIcon = itemEl.querySelector('.grid-thumb-icon');
            if (gridIcon) gridIcon.classList.add('hidden');
        }

        const listImg = itemEl.querySelector('.list-thumb-img');
        if (listImg) {
            listImg.src = src;
            listImg.classList.remove('hidden');
            const listIcon = itemEl.querySelector('.list-thumb-icon');
            if (listIcon) listIcon.classList.add('hidden');
        }
    },

    async loadThumbnails(folderId) {
        if (!folderId) return;
        const loadGen = ++FileListHandler._thumbLoadGen;
        try {
            const result = await ApiService.getThumbnails(folderId);
            if (loadGen !== FileListHandler._thumbLoadGen) return;
            if (typeof AppState !== 'undefined' && AppState.currentFolderId !== folderId) return;

            if (result && result.success && result.thumbnails) {
                AppState.currentThumbnails = result.thumbnails; // Cache for Gallery and lazy chunks
                AppState.currentThumbnailsFolderId = folderId;
                const entries = Object.entries(result.thumbnails);
                if (entries.length === 0) return;

                // Single-pass DOM index avoids repeated querySelector lookups per item
                const itemElMap = new Map();
                if (FileListView.fileListBodyEl) {
                    FileListView.fileListBodyEl.querySelectorAll('.file-item[data-type="file"]').forEach(el => {
                        itemElMap.set(String(el.dataset.id), el);
                    });
                }

                // Batch process DOM updates using requestAnimationFrame to avoid freezing UI
                const BATCH_SIZE = 20;
                let currentIndex = 0;

                const processBatch = () => {
                    if (loadGen !== FileListHandler._thumbLoadGen) return;
                    const endIndex = Math.min(currentIndex + BATCH_SIZE, entries.length);
                    for (let i = currentIndex; i < endIndex; i++) {
                        const [fileId, b64] = entries[i];
                        const itemEl = itemElMap.get(String(fileId));
                        FileListHandler._applyThumbnailToElement(itemEl, b64);
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
