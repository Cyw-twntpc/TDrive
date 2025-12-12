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

    /**
     * Initializes the FileListHandler by setting up event listeners for sorting and selection.
     * @param {Function} onSort - Callback function to execute when a sort header is clicked.
     * @param {Function} onUpdateSelection - Callback function to execute when the selection changes.
     */
    init(onSort, onUpdateSelection) {
        this.setupSortableHeaders(onSort);
        this.setupSelection(document.getElementById('file-list-container'), onUpdateSelection);
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
        
        const fragment = document.createDocumentFragment();
        contents.folders.forEach(folder => fragment.appendChild(this._createItemElement(folder, true, AppState)));
        contents.files.forEach(file => fragment.appendChild(this._createItemElement(file, false, AppState)));
        
        this.fileListBodyEl.appendChild(fragment);
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
        itemEl.draggable = true; // [Added] Enable dragging
        itemEl.dataset.id = item.id;
        itemEl.dataset.name = item.name;
        itemEl.dataset.type = isFolder ? 'folder' : 'file';
        
        if (item.isUploading) {
            itemEl.classList.add('is-uploading');
            itemEl.draggable = false; // Uploading items cannot be dragged
        }

        // --- Drag Source Logic ---
        itemEl.addEventListener('dragstart', (e) => {
            if (itemEl.classList.contains('is-uploading')) {
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
        let iconHtml = `<i class="${iconClass}"></i>`;
        if (item.isUploading) {
             iconHtml = `<i class="fas fa-spinner fa-spin"></i>`;
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

        itemEl.innerHTML = `
            <div class="file-item-col name">
                <div class="name-col-main">
                    ${iconHtml}
                    <div class="name-and-path">
                        <span>${item.name}</span>
                        ${pathHtml} 
                    </div>
                </div>
                <div class="name-col-actions">
                    <button class="item-action-btn rename-btn" title="Rename"><i class="fas fa-pencil-alt"></i></button>
                    <button class="item-action-btn move-btn" title="Move"><i class="fas fa-arrow-right-to-bracket"></i></button>
                    <button class="item-action-btn download-btn" title="Download"><i class="fas fa-download"></i></button>
                    <button class="item-action-btn delete-btn" title="Delete"><i class="fas fa-trash"></i></button>
                </div>
            </div>
            <div class="file-item-col date">${item.modif_date}</div>
            <div class="file-item-col type">${UIManager.getFileTypeDescription(item.name, isFolder)}</div>
            <div class="file-item-col size">${item.size}</div>
        `;
        
        // Add a double-click listener for folders to navigate into them.
        if (isFolder) {
            itemEl.addEventListener('dblclick', () => itemEl.dispatchEvent(new CustomEvent('folder-dblclick', { detail: { id: item.id }, bubbles: true })));
        }

        this._addSelectionListener(itemEl, item, isFolder ? 'folder' : 'file', AppState);
        
        // Dispatch custom events for actions to be handled by a central listener in main.js.
        const itemDetail = { ...item, type: isFolder ? 'folder' : 'file' };
        itemEl.querySelector('.rename-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-rename', { detail: itemDetail, bubbles: true })); });
        itemEl.querySelector('.move-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-move', { detail: itemDetail, bubbles: true })); });
        itemEl.querySelector('.download-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-download', { detail: itemDetail, bubbles: true })); });
        itemEl.querySelector('.delete-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-delete', { detail: itemDetail, bubbles: true })); });
        
        // Manually handle hover effect for action buttons because the pure CSS :hover
        // can be buggy with virtual lists and fast DOM manipulations.
        const actionsEl = itemEl.querySelector('.name-col-actions');
        itemEl.addEventListener('mouseenter', () => {
            actionsEl.style.visibility = 'visible';
            actionsEl.style.opacity = '1';
        });
        itemEl.addEventListener('mouseleave', () => {
            actionsEl.style.visibility = 'hidden';
            actionsEl.style.opacity = '0';
        });
        // [Fix] Add mousemove as a fallback to ensure buttons appear if mouseenter is missed
        itemEl.addEventListener('mousemove', () => {
            if (actionsEl.style.visibility !== 'visible') {
                actionsEl.style.visibility = 'visible';
                actionsEl.style.opacity = '1';
            }
        });

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
                    return a.name.localeCompare(b.name, 'zh-Hans-CN-u-co-pinyin') * (order === 'asc' ? 1 : -1);
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
        });
    },
    
    /**
     * Sets up the drag-to-select functionality.
     * @param {HTMLElement} containerEl - The container element for the file list.
     * @param {Function} onUpdate - Callback to notify when selection changes.
     */
    setupSelection(containerEl, onUpdate) {
        let isDragging = false, startX = 0, startY = 0;
        containerEl.addEventListener('mousedown', e => {
            // Only start dragging if the mousedown is on the container itself, not an item.
            if (e.target !== containerEl && e.target !== document.getElementById('file-list-body')) return;
            
            e.preventDefault(); 
            isDragging = true;
            const rect = containerEl.getBoundingClientRect();
            startX = e.clientX - rect.left; 
            startY = e.clientY - rect.top + containerEl.scrollTop;
            
            Object.assign(this.selectionBox.style, { left: `${startX}px`, top: `${startY - containerEl.scrollTop}px`, width: '0px', height: '0px', display: 'block' });
            
            // Clear previous selection if Ctrl is not held.
            if (!e.ctrlKey) {
                document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0;
            }

            const onMouseMove = (moveE) => {
                if (!isDragging) return;

                const currentX = moveE.clientX - rect.left;
                const currentY = moveE.clientY - rect.top + containerEl.scrollTop;
                const newLeft = Math.min(startX, currentX);
                const newTop = Math.min(startY, currentY);
                const newWidth = Math.abs(startX - currentX);
                const newHeight = Math.abs(startY - currentY);
                
                Object.assign(this.selectionBox.style, { left: `${newLeft}px`, top: `${newTop - containerEl.scrollTop}px`, width: `${newWidth}px`, height: `${newHeight}px` });
                
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
                            if (itemData) {
                                AppState.selectedItems.push({ ...itemData, type: itemType });
                            }
                        }
                    } else {
                        if (isSelected) {
                            itemEl.classList.remove('selected');
                            const indexToRemove = AppState.selectedItems.findIndex(i => i.id === itemId && i.type === itemType);
                            if (indexToRemove > -1) AppState.selectedItems.splice(indexToRemove, 1);
                        }
                    }
                });
                if (onUpdate) onUpdate(AppState);
            };

            const onMouseUp = () => {
                isDragging = false; 
                this.selectionBox.style.display = 'none';
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                if (onUpdate) onUpdate(AppState);
            };
            
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    }
};
