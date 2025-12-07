const FileListHandler = {
    // --- DOM Elements ---
    fileListBodyEl: document.getElementById('file-list-body'),
    breadcrumbEl: document.getElementById('breadcrumb'),
    selectionBox: document.getElementById('selection-box'),

    // --- Initialization ---
    init(onSort, onUpdateSelection) {
        this.setupSortableHeaders(onSort);
        this.setupSelection(document.getElementById('file-list-container'), onUpdateSelection);
    },

    // --- Rendering ---
    updateBreadcrumb(AppState, navigateTo) {
        this.breadcrumbEl.innerHTML = '';
        if (AppState.isSearching) {
            const searchHtml = `搜尋 <span class="breadcrumb-search-term">${AppState.searchTerm}</span> 的結果`;
            this.breadcrumbEl.innerHTML = searchHtml;
            return;
        }

        const path = [];
        let currentId = AppState.currentFolderId;
        
        while (currentId) {
            const folder = AppState.folderMap.get(currentId);
            if (folder) {
                path.unshift(folder);
                currentId = folder.parent_id;
            } else {
                break;
            }
        }

        path.forEach((folder, index) => {
            const isLast = index === path.length - 1;
            if (isLast) {
                this.breadcrumbEl.appendChild(Object.assign(document.createElement('span'), {
                    className: 'breadcrumb-current', textContent: folder.name
                }));
            } else {
                const link = Object.assign(document.createElement('a'), { href: '#', textContent: folder.name });
                link.addEventListener('click', (e) => { e.preventDefault(); navigateTo(folder.id); });
                this.breadcrumbEl.appendChild(link);
                this.breadcrumbEl.appendChild(Object.assign(document.createElement('span'), { className: 'separator', innerHTML: '&nbsp;&gt;&nbsp;' }));
            }
        });
    },

    _updateFileListDOM(contents, AppState) {
        this.fileListBodyEl.innerHTML = '';
        AppState.selectedItems.length = 0;
        
        const fragment = document.createDocumentFragment();
        contents.folders.forEach(folder => fragment.appendChild(this._createItemElement(folder, true, AppState)));
        contents.files.forEach(file => fragment.appendChild(this._createItemElement(file, false, AppState)));
        
        this.fileListBodyEl.appendChild(fragment);
    },
    
    _createItemElement(item, isFolder, AppState) {
        const itemEl = document.createElement('div');
        itemEl.className = 'file-item';
        itemEl.dataset.id = item.id;
        itemEl.dataset.name = item.name;
        itemEl.dataset.type = isFolder ? 'folder' : 'file';
        
        if (item.isUploading) {
            itemEl.classList.add('is-uploading');
        }

        const iconClass = isFolder ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
        
        let iconHtml = `<i class="${iconClass}"></i>`;
        if (item.isUploading) {
             iconHtml = `<i class="fas fa-spinner fa-spin"></i>`;
        }
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
                    <button class="item-action-btn rename-btn" title="重新命名"><i class="fas fa-pencil-alt"></i></button>
                    <button class="item-action-btn download-btn" title="下載"><i class="fas fa-download"></i></button>
                    <button class="item-action-btn delete-btn" title="刪除"><i class="fas fa-trash"></i></button>
                </div>
            </div>
            <div class="file-item-col date">${item.modif_date}</div>
            <div class="file-item-col type">${UIManager.getFileTypeDescription(item.name, isFolder)}</div>
            <div class="file-item-col size">${item.size}</div>
        `;
        
        if (isFolder) {
            itemEl.addEventListener('dblclick', () => itemEl.dispatchEvent(new CustomEvent('folder-dblclick', { detail: { id: item.id }, bubbles: true })));
        }

        this._addSelectionListener(itemEl, item, isFolder ? 'folder' : 'file', AppState);
        
        const itemDetail = { ...item, type: isFolder ? 'folder' : 'file' };
        itemEl.querySelector('.rename-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-rename', { detail: itemDetail, bubbles: true })); });
        itemEl.querySelector('.download-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-download', { detail: itemDetail, bubbles: true })); });
        itemEl.querySelector('.delete-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-delete', { detail: itemDetail, bubbles: true })); });
        
        return itemEl;
    },

    // --- Sorting ---
    sortAndRender(AppState) {
        const { key, order } = AppState.currentSort;
        const sorter = (a, b) => {
            const aIsFolder = a.type === 'folder';
            const bIsFolder = b.type === 'folder';

            if (aIsFolder && !bIsFolder) return -1;
            if (!aIsFolder && bIsFolder) return 1;

            let valA, valB;
            switch (key) {
                case 'name':
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
            return a.name.localeCompare(b.name);
        };

        const sortedFolders = [...(AppState.currentFolderContents.folders || [])].sort(sorter);
        const sortedFiles = [...(AppState.currentFolderContents.files || [])].sort(sorter);
        this._updateFileListDOM({ folders: sortedFolders, files: sortedFiles }, AppState);

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
    
    // --- Selection ---
    _addSelectionListener(element, item, type, AppState) { // 傳入 type
        element.addEventListener('click', (e) => {
            if (e.detail !== 1 || element.classList.contains('is-uploading')) return;

            const itemWithTpye = { ...item, type: type };

            const findIndex = () => AppState.selectedItems.findIndex(i => i.id === itemWithTpye.id && i.type === itemWithTpye.type);
            let itemIndex = findIndex();

            if (e.ctrlKey) {
                if (itemIndex > -1) {
                    element.classList.remove('selected');
                    AppState.selectedItems.splice(itemIndex, 1);
                } else {
                    element.classList.add('selected');
                    AppState.selectedItems.push(itemWithTpye);
                }
            } else {
                if (AppState.selectedItems.length === 1 && itemIndex === 0) return;

                document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0; 
                element.classList.add('selected');
                AppState.selectedItems.push(itemWithTpye);
            }
        });
    },
    
    setupSelection(containerEl, onUpdate) {
        let isDragging = false, startX = 0, startY = 0;
        containerEl.addEventListener('mousedown', e => {
            if (e.target !== containerEl && e.target !== document.getElementById('file-list-body')) return;
            
            e.preventDefault(); 
            isDragging = true;
            const rect = containerEl.getBoundingClientRect();
            startX = e.clientX - rect.left; 
            startY = e.clientY - rect.top + containerEl.scrollTop;
            
            Object.assign(this.selectionBox.style, { left: `${startX}px`, top: `${startY - containerEl.scrollTop}px`, width: '0px', height: '0px', display: 'block' });
            
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
                            let item = null;
                            if (itemType === 'folder') {
                                item = AppState.currentFolderContents.folders.find(i => i.id === itemId);
                            } else {
                                item = AppState.currentFolderContents.files.find(i => i.id === itemId);
                            }
                            if(item) {
                                AppState.selectedItems.push({ ...item, type: itemType });
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
                if(onUpdate) onUpdate(AppState);
            };

            const onMouseUp = () => {
                isDragging = false; 
                this.selectionBox.style.display = 'none';
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                if(onUpdate) onUpdate(AppState);
            };
            
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    }
};
