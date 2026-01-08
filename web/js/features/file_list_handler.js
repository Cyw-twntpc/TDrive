/**
 * @fileoverview Manages the rendering and user interactions for the main file and folder list view.
 * This includes rendering the list itself, the breadcrumb navigation, handling sorting,
 * and managing item selection (both click and drag-to-select).
 */

const FileListHandler = {
    // --- DOM Element References ---
    fileListBodyEl: document.getElementById('file-list-body'),
    breadcrumbEl: document.getElementById('breadcrumb'),
    selectionBox: document.getElementById('selection-box'),
    // Floating Toolbar Elements
    floatingToolbar: document.getElementById('file-floating-toolbar'),
    ftDownloadBtn: document.getElementById('ft-download-btn'),
    ftMoveBtn: document.getElementById('ft-move-btn'),
    ftRenameBtn: document.getElementById('ft-rename-btn'),
    ftShareBtn: document.getElementById('ft-share-btn'),
    ftDetailsBtn: document.getElementById('ft-details-btn'),
    ftTrashBtn: document.getElementById('ft-trash-btn'),

    /**
     * Initializes the FileListHandler by setting up event listeners for sorting and selection.
     * @param {Function} onSort - Callback function to execute when a sort header is clicked.
     * @param {Function} onUpdateSelection - Callback function to execute when the selection changes.
     */
    init(onSort, onUpdateSelection) {
        this.setupSortableHeaders(onSort);
        // Integrate floating toolbar update into the selection callback
        const wrappedOnUpdateSelection = (AppState) => {
            this.updateToolbarState(AppState);
            if (onUpdateSelection) onUpdateSelection(AppState);
        };
        this.setupSelection(document.getElementById('file-list-container'), wrappedOnUpdateSelection);
        this.setupFloatingToolbar();
    },

    setupFloatingToolbar() {
        if (!this.floatingToolbar) return;

        // Bind Actions
        this.ftDownloadBtn.addEventListener('click', () => ActionHandler.handleDownload());
        this.ftMoveBtn.addEventListener('click', () => ActionHandler.handleMove());
        this.ftRenameBtn.addEventListener('click', () => {
            if (AppState.selectedItems.length === 1) ActionHandler.handleRename(AppState.selectedItems[0]);
        });
        this.ftShareBtn.addEventListener('click', () => {
            UIModals.showAlert('分享', '此功能即將推出！');
        });
        // Details button placeholder
        this.ftDetailsBtn.addEventListener('click', () => {
             // Future implementation
             console.log("Details clicked");
        });
        this.ftTrashBtn.addEventListener('click', () => ActionHandler.handleDelete());
    },

    updateToolbarState(AppState) {
        if (!this.floatingToolbar) return;

        const count = AppState.selectedItems.length;
        if (count > 0) {
            this.floatingToolbar.classList.add('visible');
            
            // Rename is only allowed for single selection
            this.ftRenameBtn.disabled = (count !== 1);
            
            // Details is allowed for multiple selection (aggregate info)
            this.ftDetailsBtn.disabled = false;
        } else {
            this.floatingToolbar.classList.remove('visible');
        }
    },


    /**
     * Renders the breadcrumb navigation based on the current folder path.
     * @param {object} AppState - The global application state.
     * @param {Function} navigateTo - A callback function to handle navigation when a breadcrumb link is clicked.
     */
    updateBreadcrumb(AppState, navigateTo) {
        this.breadcrumbEl.innerHTML = '';
        if (AppState.isSearching) {
            const searchHtml = `Search results for <span class="breadcrumb-search-term">${AppState.searchTerm}</span>`;
            this.breadcrumbEl.innerHTML = searchHtml;
            return;
        }

        const path = [];
        let currentId = AppState.currentFolderId;
        
        // Traverse up the folder tree to build the path.
        while (currentId) {
            const folder = AppState.folderMap.get(currentId);
            if (folder) {
                path.unshift(folder);
                currentId = folder.parent_id;
            } else {
                break; // Stop if a parent is not found (shouldn't happen in a valid tree).
            }
        }

        path.forEach((folder, index) => {
            const isLast = index === path.length - 1;
            if (isLast) {
                // The current folder is just text, not a link.
                this.breadcrumbEl.appendChild(Object.assign(document.createElement('span'), {
                    className: 'breadcrumb-current', textContent: folder.name
                }));
            } else {
                const link = Object.assign(document.createElement('a'), { href: '#', textContent: folder.name });
                link.addEventListener('click', (e) => { e.preventDefault(); navigateTo(folder.id); });
                
                // [Added] Make breadcrumb items drop targets
                this._setupDropTarget(link, folder.id);

                this.breadcrumbEl.appendChild(link);
                this.breadcrumbEl.appendChild(Object.assign(document.createElement('span'), { className: 'separator', innerHTML: '&nbsp;&gt;&nbsp;' }));
            }
        });
    },

    /**
     * Clears and re-renders the entire file list in the DOM.
     * @param {object} contents - An object containing `folders` and `files` arrays.
     * @param {object} AppState - The global application state.
     * @private
     */
    _updateFileListDOM(contents, AppState) {
        this.fileListBodyEl.innerHTML = '';
        AppState.selectedItems.length = 0;
        this.updateToolbarState(AppState); 
        
        const isGrid = AppState.viewMode === 'grid';
        if (isGrid) {
            this.fileListBodyEl.classList.add('grid-view');
        } else {
            this.fileListBodyEl.classList.remove('grid-view');
        }

        const fragment = document.createDocumentFragment();
        const createFn = isGrid ? this._createGridItemElement.bind(this) : this._createItemElement.bind(this);

        contents.folders.forEach(folder => fragment.appendChild(createFn(folder, true, AppState)));
        contents.files.forEach(file => fragment.appendChild(createFn(file, false, AppState)));
        
        this.fileListBodyEl.appendChild(fragment);

        if (contents.files.length > 0) {
            this.loadThumbnails(AppState.currentFolderId);
        }
    },

    async loadThumbnails(folderId) {
        if (!folderId) return;
        try {
            const result = await ApiService.getThumbnails(folderId);
            console.log("[FileListHandler] loadThumbnails result:", result);
            
            if (result && result.success && result.thumbnails) {
                AppState.currentThumbnails = result.thumbnails; // Cache for Gallery
                
                Object.entries(result.thumbnails).forEach(([fileId, b64]) => {
                    const src = `data:image/jpeg;base64,${b64}`;
                    
                    // Update Grid View
                    const gridImg = this.fileListBodyEl.querySelector(`.file-item[data-id="${fileId}"] .grid-thumb-img`);
                    if (gridImg) {
                        gridImg.src = src;
                        gridImg.classList.remove('hidden');
                        const gridIcon = this.fileListBodyEl.querySelector(`.file-item[data-id="${fileId}"] .grid-thumb-icon`);
                        if (gridIcon) gridIcon.classList.add('hidden');
                    }

                    // Update List View
                    const listImg = this.fileListBodyEl.querySelector(`.file-item[data-id="${fileId}"] .list-thumb-img`);
                    if (listImg) {
                        listImg.src = src;
                        listImg.classList.remove('hidden');
                        const listIcon = this.fileListBodyEl.querySelector(`.file-item[data-id="${fileId}"] .list-thumb-icon`);
                        if (listIcon) listIcon.classList.add('hidden');
                    }
                });
            }
        } catch (e) {
            console.error("Failed to load thumbnails:", e);
        }
    },

    _createGridItemElement(item, isFolder, AppState) {
        const itemEl = document.createElement('div');
        itemEl.className = 'file-item grid-item';
        itemEl.draggable = false;
        itemEl.dataset.id = item.id;
        itemEl.dataset.name = item.name;
        itemEl.dataset.type = isFolder ? 'folder' : 'file';
        
        if (item.isUploading) itemEl.classList.add('is-uploading');

        // --- Drag & Drop Logic (Same as List) ---
        itemEl.addEventListener('mousedown', (e) => {
            if (itemEl.classList.contains('is-uploading')) return;
            if (itemEl.classList.contains('selected')) itemEl.draggable = true;
            else itemEl.draggable = false;
        });
        itemEl.addEventListener('mouseup', () => { itemEl.draggable = false; });
        
        itemEl.addEventListener('dragstart', (e) => {
            if (itemEl.classList.contains('is-uploading') || !itemEl.draggable) {
                e.preventDefault(); return;
            }
            const isSelected = itemEl.classList.contains('selected');
            if (!isSelected) {
                document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0;
                itemEl.classList.add('selected');
                AppState.selectedItems.push({ ...item, type: isFolder ? 'folder' : 'file' });
            }
            AppState.isDragging = true;
            AppState.draggedItems = [...AppState.selectedItems];
            const ghost = this._createDragGhost(AppState.draggedItems);
            document.body.appendChild(ghost);
            e.dataTransfer.setDragImage(ghost, 0, 0);
            setTimeout(() => document.body.removeChild(ghost), 0);
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', JSON.stringify(AppState.draggedItems.map(i => i.id)));
            requestAnimationFrame(() => {
                document.querySelectorAll('.file-item.selected').forEach(el => el.classList.add('dragging'));
            });
        });
        itemEl.addEventListener('dragend', (e) => {
            itemEl.draggable = false;
            AppState.isDragging = false;
            AppState.draggedItems = [];
            document.querySelectorAll('.file-item.dragging').forEach(el => el.classList.remove('dragging'));
        });

        if (isFolder) {
            this._setupDropTarget(itemEl, item.id);
            itemEl.addEventListener('dblclick', () => itemEl.dispatchEvent(new CustomEvent('folder-dblclick', { detail: { id: item.id }, bubbles: true })));
        } else {
            const ext = item.name.split('.').pop().toLowerCase();
            if (['jpg', 'jpeg', 'png', 'gif', 'webp'].includes(ext)) {
                itemEl.addEventListener('dblclick', () => {
                    itemEl.dispatchEvent(new CustomEvent('open-gallery', { detail: { id: item.id }, bubbles: true }));
                });
            }
            else if (['mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm', 'm4v', 'ts', 'mts', 'm2ts'].includes(ext)) {
                itemEl.addEventListener('dblclick', () => {
                    itemEl.dispatchEvent(new CustomEvent('play-video', { detail: { id: item.id }, bubbles: true }));
                });
            }
        }

        const iconClass = isFolder ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
        
        itemEl.innerHTML = `
            <div class="grid-thumb-container">
                <i class="${iconClass} grid-thumb-icon"></i>
                <img class="grid-thumb-img hidden" draggable="false" />
            </div>
            <div class="grid-name" title="${item.name}">${item.name}</div>
        `;

        this._addSelectionListener(itemEl, item, isFolder ? 'folder' : 'file', AppState);
        return itemEl;
    },
    
    /**
     * Creates a single DOM element for a file or folder.
     * @param {object} item - The file or folder data.
     * @param {boolean} isFolder - True if the item is a folder.
     * @param {object} AppState - The global application state.
     * @returns {HTMLElement} The created DOM element.
     * @private
     */
    _createItemElement(item, isFolder, AppState) {
        const itemEl = document.createElement('div');
        itemEl.className = 'file-item';
        itemEl.draggable = false; // [Modified] Default to false to allow click-then-drag
        itemEl.dataset.id = item.id;
        itemEl.dataset.name = item.name;
        itemEl.dataset.type = isFolder ? 'folder' : 'file';
        
        if (item.isUploading) {
            itemEl.classList.add('is-uploading');
        }

        // --- Drag Activation Logic ---
        // Only allow dragging if the item is already selected.
        itemEl.addEventListener('mousedown', (e) => {
            if (itemEl.classList.contains('is-uploading')) return;
            
            // If dragging a selected item, enable drag.
            // Otherwise, keep draggable=false to allow the mousedown to bubble to the container for marquee selection.
            if (itemEl.classList.contains('selected')) {
                itemEl.draggable = true;
            } else {
                itemEl.draggable = false;
            }
        });

        itemEl.addEventListener('mouseup', () => {
            itemEl.draggable = false;
        });

        // --- Drag Source Logic ---
        itemEl.addEventListener('dragstart', (e) => {
            if (itemEl.classList.contains('is-uploading') || !itemEl.draggable) {
                e.preventDefault();
                return;
            }

            // Selection Logic:
            // If dragging an unselected item, select it exclusively.
            // If dragging a selected item, drag all selected items.
            const isSelected = itemEl.classList.contains('selected');
            if (!isSelected) {
                // Clear previous selection
                document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0;
                // Select current
                itemEl.classList.add('selected');
                AppState.selectedItems.push({ ...item, type: isFolder ? 'folder' : 'file' });
            }

            AppState.isDragging = true;
            AppState.draggedItems = [...AppState.selectedItems];
            
            // Set Drag Image (Ghost)
            const ghost = this._createDragGhost(AppState.draggedItems);
            document.body.appendChild(ghost);
            e.dataTransfer.setDragImage(ghost, 0, 0);
            setTimeout(() => document.body.removeChild(ghost), 0); // Cleanup DOM immediately

            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', JSON.stringify(AppState.draggedItems.map(i => i.id))); // Fallback data

            // Visual feedback
            requestAnimationFrame(() => {
                document.querySelectorAll('.file-item.selected').forEach(el => el.classList.add('dragging'));
            });
        });

        itemEl.addEventListener('dragend', (e) => {
            itemEl.draggable = false; // Reset
            AppState.isDragging = false;
            AppState.draggedItems = [];
            document.querySelectorAll('.file-item.dragging').forEach(el => el.classList.remove('dragging'));
        });

        // --- Drop Target Logic (Folders only) ---
        if (isFolder) {
            this._setupDropTarget(itemEl, item.id);
        }

        // Determine the correct icon based on item type or upload status.
        const iconClass = isFolder ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
        let iconHtml = `<i class="${iconClass} list-thumb-icon"></i>`;
        
        // Thumbnail Image (Hidden by default)
        let thumbHtml = `<img class="list-thumb-img hidden" draggable="false" />`;

        if (item.isUploading) {
             iconHtml = `<i class="fas fa-spinner fa-spin list-thumb-icon"></i>`;
             thumbHtml = ''; // No thumb while uploading
        }

        // If in search mode, generate and display the item's relative path.
        let pathHtml = '';
        if (AppState.isSearching) {
            const parentPath = [];
            let currentId = item.parent_id;
            while (currentId) {
                const folder = AppState.folderMap.get(currentId);
                if (folder) {
                    parentPath.unshift(folder.name);
                    currentId = folder.parent_id;
                } else {
                    break;
                }
            }
            if (parentPath.length > 0) {
                pathHtml = `<div class="search-result-path">${parentPath.join(' / ')}</div>`;
            }
        }

        // [Feature] Smart Filename Truncation (Keep Extension)
        let nameHtml = '';
        if (isFolder) {
            nameHtml = `<span>${item.name}</span>`;
        } else {
            const lastDotIndex = item.name.lastIndexOf('.');
            if (lastDotIndex > 0 && lastDotIndex < item.name.length - 1) {
                const baseName = item.name.substring(0, lastDotIndex);
                const extName = item.name.substring(lastDotIndex);
                nameHtml = `<span class="name-part-base">${baseName}</span><span class="name-part-ext">${extName}</span>`;
            } else {
                nameHtml = `<span class="name-part-base">${item.name}</span>`;
            }
        }

        itemEl.innerHTML = `
            <div class="file-item-col name">
                <div class="name-col-main">
                    ${iconHtml}
                    ${thumbHtml}
                    <div class="name-and-path">
                        <div class="name-wrapper">${nameHtml}</div>
                        ${pathHtml} 
                    </div>
                </div>
                <div class="name-col-actions">
                    <button class="item-action-btn rename-btn" title="重新命名"><i class="fas fa-pencil-alt"></i></button>
                    <button class="item-action-btn download-btn" title="下載"><i class="fas fa-download"></i></button>
                    <button class="item-action-btn delete-btn" title="移至回收桶"><i class="fas fa-trash"></i></button>
                </div>
            </div>
            <div class="file-item-col date">${item.modif_date}</div>
            <div class="file-item-col type">${UIManager.getFileTypeDescription(item.name, isFolder)}</div>
            <div class="file-item-col size">${item.size}</div>
        `;
        
        // Add a double-click listener for folders to navigate into them.
        if (isFolder) {
            itemEl.addEventListener('dblclick', () => itemEl.dispatchEvent(new CustomEvent('folder-dblclick', { detail: { id: item.id }, bubbles: true })));
        } else {
            const ext = item.name.split('.').pop().toLowerCase();
            // Image Double Click -> Gallery
            if (['jpg', 'jpeg', 'png', 'gif', 'webp'].includes(ext)) {
                itemEl.addEventListener('dblclick', () => {
                    itemEl.dispatchEvent(new CustomEvent('open-gallery', { detail: { id: item.id }, bubbles: true }));
                });
            } 
            // Video Double Click -> Player
            else if (['mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm', 'm4v', 'ts', 'mts', 'm2ts'].includes(ext)) {
                itemEl.addEventListener('dblclick', () => {
                    itemEl.dispatchEvent(new CustomEvent('play-video', { detail: { id: item.id }, bubbles: true }));
                });
            }
        }

        this._addSelectionListener(itemEl, item, isFolder ? 'folder' : 'file', AppState);
        
        // Dispatch custom events for actions to be handled by a central listener in main.js.
        const itemDetail = { ...item, type: isFolder ? 'folder' : 'file' };
        itemEl.querySelector('.rename-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-rename', { detail: itemDetail, bubbles: true })); });
        itemEl.querySelector('.download-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-download', { detail: itemDetail, bubbles: true })); });
        itemEl.querySelector('.delete-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-delete', { detail: itemDetail, bubbles: true })); });

        return itemEl;
    },

    _createDragGhost(items) {
        const div = document.createElement('div');
        div.id = 'drag-ghost';
        const count = items.length;
        
        if (count > 1) {
            // Multiple items: Show generic icon + total count
            // Check if mixed types or all folders/files to choose icon
            const hasFolder = items.some(i => i.type === 'folder');
            const iconClass = hasFolder ? 'fas fa-folder' : 'fas fa-file'; // Or 'fas fa-layer-group'
            
            div.innerHTML = `<i class="${iconClass}"></i> <span>${count}</span>`;
        } else {
            // Single item: Show specific icon + name
            const item = items[0];
            const iconClass = item.type === 'folder' ? 'fas fa-folder' : UIManager.getFileTypeIcon(item.name);
            div.innerHTML = `<i class="${iconClass}"></i> <span>${item.name}</span>`;
        }
        
        return div;
    },

    _setupDropTarget(element, targetId) {
        element.addEventListener('dragover', (e) => {
            if (!AppState.isDragging) return;

            const isValid = ActionHandler.isValidMove(AppState.draggedItems, targetId);

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
                ActionHandler.executeMove(AppState.draggedItems, targetId);
            }
        });
    },

    /**
     * Sorts the current folder contents based on the AppState's sort key and order, then re-renders the list.
     * @param {object} AppState - The global application state.
     */
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
        this._updateFileListDOM({ folders: sortedFolders, files: sortedFiles }, AppState);

        // Update the sort indicators in the table header.
        document.querySelectorAll('.file-list-header .sortable').forEach(th => {
            th.classList.remove('asc', 'desc');
            if (th.dataset.sort === key) th.classList.add(order);
        });
    },

    /**
     * Adds click event listeners to the table headers for sorting.
     * @param {Function} onSort - The callback to execute when a sort is triggered.
     */
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
    
    /**
     * Adds a click listener to an item element for handling selection logic (single-click, ctrl+click).
     * @param {HTMLElement} element - The DOM element for the file/folder item.
     * @param {object} item - The item data object.
     * @param {string} type - 'file' or 'folder'.
     * @param {object} AppState - The global application state.
     * @private
     */
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
            this.updateToolbarState(AppState);
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

            Object.assign(this.selectionBox.style, { 
                left: `${newLeft}px`, 
                top: `${newTop}px`, 
                width: `${newWidth}px`, 
                height: `${newHeight}px` 
            });

            const boxRect = this.selectionBox.getBoundingClientRect();
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
            
            Object.assign(this.selectionBox.style, { left: `${startX}px`, top: `${startY}px`, width: '0px', height: '0px', display: 'block' });

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
                this.selectionBox.style.display = 'none';
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
    }
};
