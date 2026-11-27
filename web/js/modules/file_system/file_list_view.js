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
    ftAddPlaylistBtn: document.getElementById('ft-add-playlist-btn'),
    _renderState: {
        list: [],
        index: 0,
        chunkSize: 50,
        observer: null,
        appState: null,
        isGrid: false,
    },

    PREVIEW_EXTENSIONS: {
        image: ['jpg', 'jpeg', 'png', 'gif', 'webp'],
        video: ['mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm', 'm4v', 'ts', 'mts', 'm2ts'],
        audio: ['mp3', 'wav', 'flac', 'ogg', 'opus', 'm4a', 'wma', 'aac'],
        document: ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp'],
        text: ['py', 'js', 'ts', 'html', 'css', 'json', 'xml', 'yaml', 'yml', 'ini',
               'md', 'sh', 'bat', 'ps1', 'sql', 'c', 'cpp', 'h', 'java', 'go', 'rs',
               'rb', 'php', 'r', 'swift', 'kt', 'scala', 'lua', 'pl', 'perl', 'tex',
               'txt', 'log', 'cfg', 'conf', 'env', 'gitignore', 'dockerfile']
    },

    isAudioFile(fileName) {
        if (!fileName || !fileName.includes('.')) return false;
        const ext = fileName.split('.').pop().toLowerCase();
        return FileListView.PREVIEW_EXTENSIONS.audio.includes(ext);
    },

    /** Bind shared drag-source and drop-target behavior for list and grid file items. */
    _bindItemDragEvents(itemEl, item, isFolder, AppState) {
        itemEl.addEventListener('mousedown', () => {
            if (itemEl.classList.contains('is-uploading')) return;
            itemEl.draggable = itemEl.classList.contains('selected');
        });
        itemEl.addEventListener('mouseup', () => {
            itemEl.draggable = false;
        });

        itemEl.addEventListener('dragstart', (e) => {
            if (itemEl.classList.contains('is-uploading') || !itemEl.draggable) {
                e.preventDefault();
                return;
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

        itemEl.addEventListener('dragend', () => {
            itemEl.draggable = false;
            AppState.isDragging = false;
            AppState.draggedItems = [];
            document.querySelectorAll('.file-item.dragging').forEach(el => el.classList.remove('dragging'));
        });

        if (isFolder) {
            FileListHandler._setupDropTarget(itemEl, item.id);
        }
    },

    /** Bind shared double-click navigation or preview dispatch for list and grid items. */
    _bindItemDoubleClick(itemEl, item, isFolder) {
        if (isFolder) {
            itemEl.addEventListener('dblclick', () => {
                itemEl.dispatchEvent(new CustomEvent('folder-dblclick', { detail: { id: item.id }, bubbles: true }));
            });
            return;
        }

        const ext = item.name.split('.').pop().toLowerCase();
        const eventSpec = [
            [FileListView.PREVIEW_EXTENSIONS.image, 'open-gallery', { id: item.id }],
            [FileListView.PREVIEW_EXTENSIONS.video, 'play-video', { id: item.id }],
            [FileListView.PREVIEW_EXTENSIONS.audio, 'play-audio', { id: item.id, name: item.name }],
            [FileListView.PREVIEW_EXTENSIONS.document, 'preview-document', { id: item.id }],
            [FileListView.PREVIEW_EXTENSIONS.text, 'preview-text', { id: item.id, name: item.name }],
        ].find(([exts]) => exts.includes(ext));

        if (!eventSpec) return;

        itemEl.addEventListener('dblclick', () => {
            if (item.isUploading || itemEl.classList.contains('is-uploading')) return;
            itemEl.dispatchEvent(new CustomEvent(eventSpec[1], { detail: eventSpec[2], bubbles: true }));
        });
    },

    updateToolbarState(AppState) {
        if (!FileListView.floatingToolbar) return;

        const count = AppState.selectedItems.length;
        const countEl = document.getElementById('ft-selected-count');
        if (count > 0) {
            FileListView.floatingToolbar.classList.add('visible');
            if (countEl) {
                const tpl = window.t('file_list.selected_count');
                countEl.textContent = (tpl && tpl !== 'file_list.selected_count')
                    ? tpl.replace('{count}', String(count))
                    : `${count}`;
            }
            
            // Rename is only allowed for single selection
            FileListView.ftRenameBtn.disabled = (count !== 1);
            
            // Details is allowed for multiple selection (aggregate info)
            FileListView.ftDetailsBtn.disabled = false;

            const addPlBtn = FileListView.ftAddPlaylistBtn || document.getElementById('ft-add-playlist-btn');
            if (addPlBtn) {
                const hasAudio = AppState.selectedItems.some(
                    item => item.type === 'file' && FileListView.isAudioFile(item.name)
                );
                addPlBtn.classList.toggle('hidden', !hasAudio);
            }
        } else {
            FileListView.floatingToolbar.classList.remove('visible');
            const addPlBtn = FileListView.ftAddPlaylistBtn || document.getElementById('ft-add-playlist-btn');
            if (addPlBtn) {
                addPlBtn.classList.add('hidden');
            }
        }
    },

    updateBreadcrumb(AppState, navigateTo) {
        FileListView.breadcrumbEl.innerHTML = '';
        if (AppState.isSearching) {
            const i18nPrefix = window.t('main.search_results_for');
            const prefix = (i18nPrefix && i18nPrefix !== 'main.search_results_for') ? i18nPrefix : 'Search results for ';
            FileListView.breadcrumbEl.appendChild(document.createTextNode(prefix));
            const termSpan = document.createElement('span');
            termSpan.className = 'breadcrumb-search-term';
            termSpan.textContent = AppState.searchTerm;
            FileListView.breadcrumbEl.appendChild(termSpan);
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
        if (FileListView._renderState.observer) {
            FileListView._renderState.observer.disconnect();
            FileListView._renderState.observer = null;
        }

        // Prepare render list
        const renderList = [];
        contents.folders.forEach(f => renderList.push({ item: f, isFolder: true }));
        contents.files.forEach(f => renderList.push({ item: f, isFolder: false }));

        FileListView._renderState.list = renderList;
        FileListView._renderState.index = 0;
        FileListView._renderState.appState = AppState;
        FileListView._renderState.isGrid = isGrid;

        FileListView._renderNextChunk();

        if (contents.files.length > 0) {
            FileListHandler.loadThumbnails(AppState.currentFolderId);
        }
    },

    _renderNextChunk() {
        const state = FileListView._renderState;
        if (state.index >= state.list.length) return;

        const fragment = document.createDocumentFragment();
        const createFn = state.isGrid ? FileListView._createGridItemElement.bind(FileListView) : FileListView._createItemElement.bind(FileListView);

        // Remove old sentinel if exists
        const oldSentinel = FileListView.fileListBodyEl.querySelector('.scroll-sentinel');
        if (oldSentinel) oldSentinel.remove();

        const endIndex = Math.min(state.index + state.chunkSize, state.list.length);
        const hasFolderThumbCache = Boolean(
            state.appState &&
            state.appState.currentThumbnails &&
            state.appState.currentThumbnailsFolderId === state.appState.currentFolderId
        );
        for (let i = state.index; i < endIndex; i++) {
            const data = state.list[i];
            const el = createFn(data.item, data.isFolder, state.appState);
            if (!data.isFolder && hasFolderThumbCache && typeof FileListHandler !== 'undefined' && typeof FileListHandler._applyThumbnailToElement === 'function') {
                const cachedThumb = state.appState.currentThumbnails[data.item.id];
                if (cachedThumb) {
                    FileListHandler._applyThumbnailToElement(el, cachedThumb);
                }
            }
            fragment.appendChild(el);
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
        itemEl.className = 'list-item file-item grid-item';
        itemEl.draggable = false;
        itemEl.dataset.id = item.id;
        itemEl.dataset.name = item.name;
        itemEl.dataset.type = isFolder ? 'folder' : 'file';
        
        if (item.isUploading) itemEl.classList.add('is-uploading');

        FileListView._bindItemDragEvents(itemEl, item, isFolder, AppState);
        FileListView._bindItemDoubleClick(itemEl, item, isFolder);

        const iconClass = isFolder ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
        const tileClass = typeof UIManager.getFileTileClass === 'function'
            ? UIManager.getFileTileClass(item.name, isFolder)
            : (isFolder ? 'tile-folder' : 'tile-default');

        itemEl.innerHTML = `
            <div class="grid-thumb-container">
                <div class="icon-tile ${tileClass}">
                    <i class="${iconClass} grid-thumb-icon"></i>
                </div>
                <img class="grid-thumb-img hidden" draggable="false" />
            </div>
            <div class="grid-name" data-tooltip="${UIManager.escapeHtml(item.name)}" data-tooltip-truncated="true">${UIManager.escapeHtml(item.name)}</div>
        `;

        FileListHandler._addSelectionListener(itemEl, item, isFolder ? 'folder' : 'file', AppState);
        return itemEl;
    },

    _createItemElement(item, isFolder, AppState) {
        const itemEl = document.createElement('div');
        itemEl.className = 'list-item file-item';
        itemEl.draggable = false; // [Modified] Default to false to allow click-then-drag
        itemEl.dataset.id = item.id;
        itemEl.dataset.name = item.name;
        itemEl.dataset.type = isFolder ? 'folder' : 'file';
        
        if (item.isUploading) {
            itemEl.classList.add('is-uploading');
        }

        FileListView._bindItemDragEvents(itemEl, item, isFolder, AppState);

        // Determine the correct icon based on item type or upload status.
        const iconClass = isFolder ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
        const tileClass = typeof UIManager.getFileTileClass === 'function'
            ? UIManager.getFileTileClass(item.name, isFolder)
            : (isFolder ? 'tile-folder' : 'tile-default');
        let iconHtml = `<i class="${iconClass} list-thumb-icon"></i>`;

        // Thumbnail Image (Hidden by default)
        let thumbHtml = `<img class="list-thumb-img hidden" draggable="false" />`;

        if (item.isUploading) {
            iconHtml = `<i class="fas fa-spinner fa-spin list-thumb-icon"></i>`;
            thumbHtml = ''; // No thumb while uploading
        }
        const tileHtml = `<div class="icon-tile ${tileClass}">${iconHtml}${thumbHtml}</div>`;

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

        const addPlaylistBtnHtml = (!isFolder && FileListView.isAudioFile(item.name))
            ? `<button class="item-action-btn add-playlist-btn" data-tooltip="${window.t('music.add_to_playlist')}"><i class="fas fa-plus-circle"></i></button>`
            : '';

        itemEl.innerHTML = `
            <div class="col file-item-col name">
                <div class="name-col-main">
                    ${tileHtml}
                    <div class="name-and-path">
                        <div class="name-wrapper">${nameHtml}</div>
                        ${pathHtml} 
                    </div>
                </div>
                <div class="name-col-actions">
                    ${addPlaylistBtnHtml}
                    <button class="item-action-btn rename-btn" data-tooltip="${window.t('menu.rename')}"><i class="fas fa-pencil-alt"></i></button>
                    <button class="item-action-btn download-btn" data-tooltip="${window.t('menu.download')}"><i class="fas fa-download"></i></button>
                    <button class="item-action-btn delete-btn" data-tooltip="${window.t('menu.move_to_trash')}"><i class="fas fa-trash"></i></button>
                </div>
            </div>
            <div class="col file-item-col date">${item.modif_date}</div>
            <div class="col file-item-col type">${UIManager.getFileTypeDescription(item.name, isFolder)}</div>
            <div class="col file-item-col size">${item.size}</div>
        `;
        
        FileListView._bindItemDoubleClick(itemEl, item, isFolder);

        FileListHandler._addSelectionListener(itemEl, item, isFolder ? 'folder' : 'file', AppState);
        
        // Dispatch custom events for actions to be handled by a central listener in main.js.
        const itemDetail = { ...item, type: isFolder ? 'folder' : 'file' };
        const addPlBtn = itemEl.querySelector('.add-playlist-btn');
        if (addPlBtn) {
            addPlBtn.addEventListener('click', e => {
                e.stopPropagation();
                itemEl.dispatchEvent(new CustomEvent('item-add-playlist', {
                    detail: { ids: [item.id], names: [item.name] },
                    bubbles: true
                }));
            });
        }
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
