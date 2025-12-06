const UIHandler = {
    // --- DOM 元素 ---
    fileTreeEl: document.getElementById('file-tree'),
    fileListBodyEl: document.getElementById('file-list-body'),
    breadcrumbEl: document.getElementById('breadcrumb'),
    selectionBox: document.getElementById('selection-box'),
    deleteBtn: document.getElementById('delete-btn'),
    downloadBtn: document.getElementById('download-btn'),

    // --- UI 輔助函式 ---
    getFileTypeIcon(fileName) {
        const extension = fileName.split('.').pop().toLowerCase();
        if (fileName.includes('.') === false) return 'fa-solid fa-file';
        switch (extension) {
            case 'txt': case 'md': return 'fa-solid fa-file-lines';
            case 'pdf': return 'fa-solid fa-file-pdf';
            case 'doc': case 'docx': return 'fa-solid fa-file-word';
            case 'xls': case 'xlsx': return 'fa-solid fa-file-excel';
            case 'ppt': case 'pptx': return 'fa-solid fa-file-powerpoint';
            case 'zip': case 'rar': case '7z': case 'tar': return 'fa-solid fa-file-zipper';
            case 'jpg': case 'jpeg': case 'png': case 'gif': return 'fa-solid fa-file-image';
            case 'mp3': case 'wav': return 'fa-solid fa-file-audio';
            case 'mp4': case 'mov': case 'avi': return 'fa-solid fa-file-video';
            case 'py': case 'js': case 'html': case 'css': case 'json': return 'fa-solid fa-file-code';
            default: return 'fa-solid fa-file';
        }
    },
    
    formatBytes(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    },

    getFileTypeDescription(fileName, isFolder) {
        if (isFolder) return '檔案資料夾';
        const extension = fileName.split('.').pop().toLowerCase();
        if (fileName.includes('.') === false) return '檔案';
        switch (extension) {
            case 'txt': return '純文字檔案';
            case 'md': return 'Markdown 文件';
            case 'pdf': return 'PDF 文件';
            case 'doc': case 'docx': return 'Word 文件';
            case 'xls': case 'xlsx': return 'Excel 工作表';
            case 'ppt': case 'pptx': return 'PowerPoint 簡報';
            case 'zip': case 'rar': case '7z': case 'tar': return `${extension.toUpperCase()} 壓縮檔`;
            case 'jpg': case 'jpeg': return 'JPEG 影像';
            case 'png': return 'PNG 影像';
            case 'gif': return 'GIF 影像';
            case 'mp3': case 'wav': case 'aac': return `${extension.toUpperCase()} 音訊`;
            case 'mp4': case 'mov': case 'flv': case 'mkv': case 'wmv': case 'avi': return `${extension.toUpperCase()} 影片`;
            case 'py': return 'Python 程式碼';
            case 'js': return 'JavaScript 程式碼';
            case 'html': return 'HTML 文件';
            case 'css': return '網頁樣式表';
            case 'json': return 'JSON 檔案';
            case 'exe': return 'EXE 執行檔';
            default: return `${extension.toUpperCase()} 檔案`;
        }
    },

    // --- 檔案樹建構 ---
    renderFileTree(AppState, navigateTo) {
        this.fileTreeEl.innerHTML = '';
        if (!AppState.folderTreeData || AppState.folderTreeData.length === 0) {
            return;
        }

        // --- Smart Expansion Logic ---
        // 1. Create a set of all ancestors of the current folder.
        const ancestors = new Set();
        let currentId = AppState.currentFolderId;
        while (currentId) {
            ancestors.add(currentId);
            const folder = AppState.folderMap.get(currentId);
            currentId = folder ? folder.parent_id : null;
        }
    
        const childrenOf = new Map();
        AppState.folderTreeData.forEach(folder => {
            const parentId = folder.parent_id;
            if (!childrenOf.has(parentId)) {
                childrenOf.set(parentId, []);
            }
            childrenOf.get(parentId).push(folder.id);
        });
    
        const rootFolder = AppState.folderTreeData.find(f => f.parent_id === null);
    
        if (rootFolder) {
            // 2. Pass the ancestor set down the build process.
            const rootItem = this._buildTreeItem(rootFolder, AppState, navigateTo, childrenOf, ancestors, true);
            const rootUl = document.createElement('ul');
            rootUl.appendChild(rootItem);
            this.fileTreeEl.appendChild(rootUl);
        } else {
            console.error("Could not find root folder to render file tree.");
        }
    },
    
    _buildTreeItem(folder, AppState, navigateTo, childrenOf, ancestors, isRoot = false) {
        const li = document.createElement('li');
        const itemDiv = document.createElement('div');
        itemDiv.className = 'tree-item';
        itemDiv.dataset.id = folder.id;
    
        const hasSubFolders = childrenOf.has(folder.id) && childrenOf.get(folder.id).length > 0;
    
        const toggle = document.createElement('span');
        toggle.className = 'folder-toggle';
        if (hasSubFolders) {
            toggle.innerHTML = '<i class="fas fa-caret-right"></i>';
            toggle.addEventListener('click', (e) => {
                e.stopPropagation();
                const subTree = li.querySelector('ul');
                if (subTree) {
                    subTree.classList.toggle('collapsed');
                    toggle.classList.toggle('open');
                }
            });
        }
    
        const contentDiv = document.createElement('div');
        contentDiv.className = 'folder-content';
        const icon = isRoot ? 'fa-hdd' : 'fa-folder';
        contentDiv.innerHTML = `<i class="fas ${icon} folder-icon"></i><span class="folder-name">${folder.name}</span>`;
        contentDiv.addEventListener('click', () => navigateTo(folder.id));
    
        itemDiv.appendChild(toggle);
        itemDiv.appendChild(contentDiv);
        li.appendChild(itemDiv);
    
        if (hasSubFolders) {
            const ul = document.createElement('ul');
            // 3. If the current folder is NOT an ancestor, it should be collapsed by default.
            if (!ancestors.has(folder.id)) {
                ul.classList.add('collapsed');
            } else {
                toggle.classList.add('open');
            }

            const sortedChildrenIds = childrenOf.get(folder.id).sort((a, b) => {
                const nameA = AppState.folderMap.get(a).name;
                const nameB = AppState.folderMap.get(b).name;
                return nameA.localeCompare(nameB, 'zh-Hans-CN-u-co-pinyin');
            });
    
            sortedChildrenIds.forEach(childId => {
                const childFolder = AppState.folderMap.get(childId);
                const childLi = this._buildTreeItem(childFolder, AppState, navigateTo, childrenOf, ancestors);
                ul.appendChild(childLi);
            });
            li.appendChild(ul);
        }
    
        return li;
    },
    
    updateTreeSelection(AppState) {
        this.fileTreeEl.querySelector('.tree-item.active')?.classList.remove('active');
        const newActive = this.fileTreeEl.querySelector(`.tree-item[data-id="${AppState.currentFolderId}"]`);
        if (newActive) {
            newActive.classList.add('active');
        }
    },
    
    // --- 檔案列表與麵包屑渲染 ---
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

    updateFileList(contents, AppState) {
        console.log('[DEBUG] updateFileList received raw contents from backend:', JSON.parse(JSON.stringify(contents)));
        this.fileListBodyEl.innerHTML = '';
        AppState.selectedItems.length = 0; // Correct way to clear the array
        
        const fragment = document.createDocumentFragment();
        contents.folders.forEach(folder => fragment.appendChild(this.createItemElement(folder, true, AppState)));
        contents.files.forEach(file => fragment.appendChild(this.createItemElement(file, false, AppState)));
        
        this.fileListBodyEl.appendChild(fragment);
    },
    
    createItemElement(item, isFolder, AppState) {
        const itemEl = document.createElement('div');
        itemEl.className = 'file-item';
        itemEl.dataset.id = item.id;
        itemEl.dataset.name = item.name;
        itemEl.dataset.type = isFolder ? 'folder' : 'file';
        
        if (item.isUploading) {
            itemEl.classList.add('is-uploading');
        }

        const iconClass = isFolder ? 'fas fa-folder folder-icon' : this.getFileTypeIcon(item.name);
        
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
            <div class="file-item-col type">${this.getFileTypeDescription(item.name, isFolder)}</div>
            <div class="file-item-col size">${item.size}</div>
        `;
        
        if (isFolder) {
            itemEl.addEventListener('dblclick', () => itemEl.dispatchEvent(new CustomEvent('folder-dblclick', { detail: { id: item.id }, bubbles: true })));
        }

        this.addSelectionListener(itemEl, item, AppState);
        
        const itemDetail = { id: item.id, name: item.name, type: isFolder ? 'folder' : 'file' };
        itemEl.querySelector('.rename-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-rename', { detail: itemDetail, bubbles: true })); });
        itemEl.querySelector('.download-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-download', { detail: itemDetail, bubbles: true })); });
        itemEl.querySelector('.delete-btn').addEventListener('click', e => { e.stopPropagation(); itemEl.dispatchEvent(new CustomEvent('item-delete', { detail: itemDetail, bubbles: true })); });
        
        return itemEl;
    },

    // --- 排序與選取 ---
    sortAndRenderList(AppState) {
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
                    valA = this.getFileTypeDescription(a.name, a.type === 'folder');
                    valB = this.getFileTypeDescription(b.name, b.type === 'folder');
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
        this.updateFileList({ folders: sortedFolders, files: sortedFiles }, AppState);

        document.querySelectorAll('.file-list-header .sortable').forEach(th => {
            th.classList.remove('asc', 'desc');
            if (th.dataset.sort === key) th.classList.add(order);
        });
    },

    setupSortableHeaders(AppState, onSort) {
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
    
    addSelectionListener(element, item, AppState) {
        element.addEventListener('click', (e) => {
            if (e.detail !== 1 || element.classList.contains('is-uploading')) return;

            const itemId = item.id;
            const findIndex = () => AppState.selectedItems.findIndex(i => i.id === itemId);
            const itemIndex = findIndex();

            if (e.ctrlKey) {
                if (itemIndex > -1) {
                    // Item is already selected, so unselect it
                    element.classList.remove('selected');
                    AppState.selectedItems.splice(itemIndex, 1);
                } else {
                    // Item is not selected, so select it
                    element.classList.add('selected');
                    AppState.selectedItems.push(item);
                }
            } else {
                // Normal click
                if (AppState.selectedItems.length === 1 && itemIndex === 0) return;

                document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0; // Clear array
                element.classList.add('selected');
                AppState.selectedItems.push(item);
            }
        });
    },
    
    selectSingleItem(itemId, AppState) {
        document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
        AppState.selectedItems.length = 0;
        
        const itemEl = this.fileListBodyEl.querySelector(`.file-item[data-id="${itemId}"]`);
        
        // Find the full item object from the current view to push into the selection
        const folderItem = AppState.currentFolderContents.folders.find(i => i.id === itemId);
        if (folderItem) {
            AppState.selectedItems.push(folderItem);
        } else {
            const fileItem = AppState.currentFolderContents.files.find(i => i.id === itemId);
            if (fileItem) AppState.selectedItems.push(fileItem);
        }

        if (itemEl) {
            itemEl.classList.add('selected');
        }
    },

    setupSelection(containerEl, AppState, onUpdate) {
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
                    const isSelected = AppState.selectedItems.some(i => i.id === itemId);

                    if (intersects) {
                        if (!isSelected) {
                            itemEl.classList.add('selected');
                            // Find the full item object to add
                            const item = AppState.currentFolderContents.folders.find(i => i.id === itemId) || AppState.currentFolderContents.files.find(i => i.id === itemId);
                            if(item) AppState.selectedItems.push(item);
                        }
                    } else {
                        if (isSelected && !e.ctrlKey) {
                            itemEl.classList.remove('selected');
                            // Filter out the item
                            const indexToRemove = AppState.selectedItems.findIndex(i => i.id === itemId);
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
    },

    // --- 彈出式視窗與錯誤處理 ---
    updateUserAvatar(AppState) {
        const userBtn = document.getElementById('user-btn');
        if (AppState.userAvatar) {
            userBtn.innerHTML = `<img src="${AppState.userAvatar}" alt="avatar">`;
        } else {
            userBtn.innerHTML = `<i class="fas fa-user-circle"></i>`;
        }
    },

    populateUserInfoPopover(AppState) {
        const contentEl = document.getElementById('user-info-content');
        if (AppState.userInfo) {
            const info = AppState.userInfo;
            contentEl.innerHTML = `<p><strong>姓名:</strong> <span>${info.name}</span></p>
                                   <p><strong>電話:</strong> <span>${info.phone}</span></p>
                                   <p><strong>使用者名稱:</strong> <span>${info.username}</span></p>
                                   <p><strong>儲存群組:</strong> <span>${info.storage_group}</span></p>`;
        } else {
            contentEl.innerHTML = '<p>載入中...</p>';
        }
    },

    setupPopovers() {
        document.querySelectorAll('[data-popover]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const popoverId = btn.dataset.popover;
                const targetPopover = document.getElementById(popoverId);
                
                const isVisible = !targetPopover.classList.contains('hidden');
                document.querySelectorAll('.popover').forEach(p => p.classList.add('hidden'));
                if (!isVisible) {
                    targetPopover.classList.remove('hidden');
                }
            });
        });

        document.addEventListener('click', (e) => {
            if (!e.target.closest('.popover') && !e.target.closest('[data-popover]')) {
                document.querySelectorAll('.popover').forEach(p => p.classList.add('hidden'));
            }
        });
    },

    showInlineError(inputId, message) {
        const errorEl = document.getElementById(`${inputId}Error`);
        const inputEl = document.getElementById(inputId);
        if (errorEl) {
            errorEl.textContent = message ? `⚠ ${message}` : '';
            errorEl.classList.toggle('show', !!message);
        }
        if (inputEl) {
            inputEl.classList.toggle('error', !!message);
        }
    },

    handleBackendError(response) {
        let title = '發生錯誤';
        let message = response.message || '發生未知的內部錯誤，請稍後再試。';

        switch (response.error_code) {
            case 'ITEM_ALREADY_EXISTS':
                title = '操作失敗';
                break;
            case 'PATH_NOT_FOUND':
                title = '找不到項目';
                break;
            case 'CONNECTION_FAILED':
                title = '連線錯誤';
                message = '無法連接到伺服器，請檢查您的網路連線或稍後再試。';
                break;
            case 'FLOOD_WAIT_ERROR':
                title = '請求過於頻繁';
                break;
            case 'INTERNAL_ERROR':
                title = '系統內部錯誤';
                break;
        }
        this.showAlert(title, message, 'btn-primary');
    },

    showConfirm(title, message, okClass = 'btn-danger') {
        return new Promise(resolve => {
            const modal = document.getElementById('confirm-modal');
            modal.querySelector('#confirm-title').textContent = title;
            modal.querySelector('#confirm-message').innerHTML = message;
            const okBtn = modal.querySelector('#confirm-ok-btn');
            okBtn.className = `btn ${okClass}`;

            const onOk = () => { cleanup(); resolve(true); };
            const onClose = () => { cleanup(); resolve(false); };
            
            const cleanup = () => {
                modal.classList.add('hidden');
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#confirm-cancel-btn').removeEventListener('click', onClose);
                modal.querySelector('#confirm-close-btn').removeEventListener('click', onClose);
            };

            okBtn.addEventListener('click', onOk);
            modal.querySelector('#confirm-cancel-btn').addEventListener('click', onClose);
            modal.querySelector('#confirm-close-btn').addEventListener('click', onClose);
            
            modal.classList.remove('hidden');
        });
    },
    
    showAlert(title, message, okClass = 'btn-primary') {
        return new Promise(resolve => {
            const modal = document.getElementById('alert-modal');
            modal.querySelector('#alert-title').textContent = title;
            modal.querySelector('#alert-message').innerHTML = message;
            const okBtn = modal.querySelector('#alert-ok-btn');
            okBtn.className = `btn ${okClass}`;

            const onOk = () => { cleanup(); resolve(true); };
            
            const cleanup = () => {
                modal.classList.add('hidden');
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#alert-close-btn').removeEventListener('click', onOk);
            };

            okBtn.addEventListener('click', onOk);
            modal.querySelector('#alert-close-btn').addEventListener('click', onOk);
            
            modal.classList.remove('hidden');
        });
    },
    
    async showPrompt(title, message, defaultValue = '', asyncValidator = null) {
        return new Promise(resolve => {
            const modal = document.getElementById('prompt-modal');
            const input = modal.querySelector('#prompt-input');
            const errorEl = modal.querySelector('#prompt-error');
            const okBtn = modal.querySelector('#prompt-ok-btn');
            
            modal.querySelector('#prompt-title').textContent = title;
            modal.querySelector('#prompt-message').textContent = message;
            input.value = defaultValue;
            errorEl.classList.add('hidden');
            errorEl.textContent = ''; // 每次開啟時就清除
            okBtn.disabled = false;
            okBtn.classList.remove('loading');

            const onOk = async () => {
                const value = input.value.trim();
                errorEl.classList.add('hidden');
                errorEl.textContent = '';

                if (!value) {
                    errorEl.textContent = '名稱不可為空。';
                    errorEl.style.color = 'var(--danger-color)'; // 強制設定顏色
                    errorEl.classList.remove('hidden');
                    return;
                }

                if (asyncValidator) {
                    okBtn.classList.add('loading');
                    okBtn.disabled = true;
                    const result = await asyncValidator(value);
                    okBtn.classList.remove('loading');
                    okBtn.disabled = false;

                    if (result && !result.success) {
                        errorEl.textContent = result.message || '發生未知驗證錯誤。';
                        errorEl.style.color = 'var(--danger-color)'; // 強制設定顏色
                        errorEl.classList.remove('hidden');
                        return; // 保持彈窗開啟
                    }
                }
                
                cleanup();
                resolve(value);
            };

            const onClose = () => { cleanup(); resolve(null); };
            
            const onKeyPress = (e) => {
                if (e.key === 'Enter') onOk();
            };

            const onInput = () => {
                errorEl.classList.add('hidden');
                errorEl.textContent = '';
            };

            const cleanup = () => {
                modal.classList.add('hidden');
                errorEl.textContent = ''; // 關閉時也清除
                input.removeEventListener('keydown', onKeyPress);
                input.removeEventListener('input', onInput);
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#prompt-cancel-btn').removeEventListener('click', onClose);
                modal.querySelector('#prompt-close-btn').removeEventListener('click', onClose);
            };

            input.addEventListener('keydown', onKeyPress);
            input.addEventListener('input', onInput);
            okBtn.addEventListener('click', onOk);
            modal.querySelector('#prompt-cancel-btn').addEventListener('click', onClose);
            modal.querySelector('#prompt-close-btn').addEventListener('click', onClose);

            modal.classList.remove('hidden');
            input.focus();
            input.select();
        });
    }
};