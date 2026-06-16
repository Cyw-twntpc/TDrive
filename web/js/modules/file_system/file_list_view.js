const FileListView = {
    fileListBodyEl: document.getElementById('file-list-body'),
    breadcrumbEl: document.getElementById('breadcrumb'),
    selectionBox: document.getElementById('selection-box'),
    floatingToolbar: document.getElementById('file-floating-toolbar'),
    ftDownloadBtn: document.getElementById('ft-download-btn'),
    ftMoveBtn: document.getElementById('ft-move-btn'),
    ftRenameBtn: document.getElementById('ft-rename-btn'),
    ftShareBtn: document.getElementById('ft-share-btn'),
    ftDetailsBtn: document.getElementById('ft-details-btn'),
    ftTrashBtn: document.getElementById('ft-trash-btn'),

    updateToolbarState(AppState) {
        if (!FileListView.floatingToolbar) return;

        const count = AppState.selectedItems.length;
        if (count > 0) {
            FileListView.floatingToolbar.classList.add('visible');
            
            // Rename is only allowed for single selection
            FileListView.ftRenameBtn.disabled = (count !== 1);
            
            // Details is allowed for multiple selection (aggregate info)
            FileListView.ftDetailsBtn.disabled = false;
        } else {
            FileListView.floatingToolbar.classList.remove('visible');
        }
    },

    updateBreadcrumb(AppState, navigateTo) {
        FileListView.breadcrumbEl.innerHTML = '';
        if (AppState.isSearching) {
            const searchHtml = `Search results for <span class="breadcrumb-search-term">${AppState.searchTerm}</span>`;
            FileListView.breadcrumbEl.innerHTML = searchHtml;
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
                FileListView.breadcrumbEl.appendChild(Object.assign(document.createElement('span'), {
                    className: 'breadcrumb-current', textContent: folder.name
                }));
            } else {
                const link = Object.assign(document.createElement('a'), { href: '#', textContent: folder.name });
                link.addEventListener('click', (e) => { e.preventDefault(); navigateTo(folder.id); });
                
                // [Added] Make breadcrumb items drop targets
                FileListHandler._setupDropTarget(link, folder.id);

                FileListView.breadcrumbEl.appendChild(link);
                FileListView.breadcrumbEl.appendChild(Object.assign(document.createElement('span'), { className: 'separator', innerHTML: '&nbsp;&gt;&nbsp;' }));
            }
        });
    },

    _updateFileListDOM(contents, AppState) {
        FileListView.fileListBodyEl.innerHTML = '';
        AppState.selectedItems.length = 0;
        FileListView.updateToolbarState(AppState); 
        
        const isGrid = AppState.viewMode === 'grid';
        if (isGrid) {
            FileListView.fileListBodyEl.classList.add('grid-view');
        } else {
            FileListView.fileListBodyEl.classList.remove('grid-view');
        }

        // Clean up previous observer
        if (FileListModel._renderState.observer) {
            FileListModel._renderState.observer.disconnect();
            FileListModel._renderState.observer = null;
        }

        // Prepare render list
        const renderList = [];
        contents.folders.forEach(f => renderList.push({ item: f, isFolder: true }));
        contents.files.forEach(f => renderList.push({ item: f, isFolder: false }));

        FileListModel._renderState.list = renderList;
        FileListModel._renderState.index = 0;
        FileListModel._renderState.appState = AppState;
        FileListModel._renderState.isGrid = isGrid;

        FileListView._renderNextChunk();

        if (contents.files.length > 0) {
            FileListHandler.loadThumbnails(AppState.currentFolderId);
        }
    },

    _renderNextChunk() {
        const state = FileListModel._renderState;
        if (state.index >= state.list.length) return;

        const fragment = document.createDocumentFragment();
        const createFn = state.isGrid ? FileListView._createGridItemElement.bind(FileListView) : FileListView._createItemElement.bind(FileListView);

        // Remove old sentinel if exists
        const oldSentinel = FileListView.fileListBodyEl.querySelector('.scroll-sentinel');
        if (oldSentinel) oldSentinel.remove();

        const endIndex = Math.min(state.index + state.chunkSize, state.list.length);
        for (let i = state.index; i < endIndex; i++) {
            const data = state.list[i];
            fragment.appendChild(createFn(data.item, data.isFolder, state.appState));
        }
        state.index = endIndex;
        FileListView.fileListBodyEl.appendChild(fragment);

        // Add sentinel for IntersectionObserver if there are more items
        if (state.index < state.list.length) {
            const sentinel = document.createElement('div');
            sentinel.className = 'scroll-sentinel';
            sentinel.style.height = '10px'; // invisible trigger area
            FileListView.fileListBodyEl.appendChild(sentinel);

            if (!state.observer) {
                state.observer = new IntersectionObserver((entries) => {
                    if (entries[0].isIntersecting) {
                        FileListView._renderNextChunk();
                    }
                }, { root: document.getElementById('file-list-container'), rootMargin: '100px' });
            }
            state.observer.observe(sentinel);
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
            const ghost = FileListView._createDragGhost(AppState.draggedItems);
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
            FileListHandler._setupDropTarget(itemEl, item.id);
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
            <div class="grid-name" title="${UIManager.escapeHtml(item.name)}">${UIManager.escapeHtml(item.name)}</div>
        `;

        FileListHandler._addSelectionListener(itemEl, item, isFolder ? 'folder' : 'file', AppState);
        return itemEl;
    },

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
            const ghost = FileListView._createDragGhost(AppState.draggedItems);
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
            FileListHandler._setupDropTarget(itemEl, item.id);
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
                pathHtml = `<div class="search-result-path">${parentPath.map(UIManager.escapeHtml).join(' / ')}</div>`;
            }
        }

        // [Feature] Smart Filename Truncation (Keep Extension)
        let nameHtml = '';
        if (isFolder) {
            nameHtml = `<span>${UIManager.escapeHtml(item.name)}</span>`;
        } else {
            const lastDotIndex = item.name.lastIndexOf('.');
            if (lastDotIndex > 0 && lastDotIndex < item.name.length - 1) {
                const baseName = UIManager.escapeHtml(item.name.substring(0, lastDotIndex));
                const extName = UIManager.escapeHtml(item.name.substring(lastDotIndex));
                nameHtml = `<span class="name-part-base">${baseName}</span><span class="name-part-ext">${extName}</span>`;
            } else {
                nameHtml = `<span class="name-part-base">${UIManager.escapeHtml(item.name)}</span>`;
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
                    <button class="item-action-btn rename-btn" title="${window.t('menu.rename')}"><i class="fas fa-pencil-alt"></i></button>
                    <button class="item-action-btn download-btn" title="${window.t('menu.download')}"><i class="fas fa-download"></i></button>
                    <button class="item-action-btn delete-btn" title="${window.t('menu.move_to_trash')}"><i class="fas fa-trash"></i></button>
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

        FileListHandler._addSelectionListener(itemEl, item, isFolder ? 'folder' : 'file', AppState);
        
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
            div.innerHTML = `<i class="${iconClass}"></i> <span>${UIManager.escapeHtml(item.name)}</span>`;
        }
        
        return div;
    },

};
