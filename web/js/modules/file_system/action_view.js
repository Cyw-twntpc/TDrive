const ActionView = {
    showMoveModal(appState, uiManager, uiModals, onConfirmCb) {
        const modalId = 'move-modal';
        const treeContainer = document.getElementById('move-tree-container');
        const confirmBtn = document.getElementById('move-confirm-btn');
        const cancelBtn = document.getElementById('move-cancel-btn');
        const closeBtn = document.getElementById('move-close-btn');
        let selectedTargetId = null;

        const getPathToCurrent = () => {
            const path = [];
            let current = appState.currentFolderId;
            while (current) {
                path.unshift(current);
                const folder = appState.folderMap.get(current);
                current = folder ? folder.parent_id : null;
            }
            return path;
        };
        const expandedIds = new Set(getPathToCurrent());

        const renderMoveTree = () => {
            treeContainer.innerHTML = '';
            const roots = appState.folderTreeData.filter(f => f.parent_id === null);

            const isBeingMoved = (folderId) => {
                return appState.selectedItems.some(i => i.type === 'folder' && i.id === folderId);
            };

            const createNode = (folder, level) => {
                if (isBeingMoved(folder.id)) return null;
                const nodeEl = document.createElement('div');
                nodeEl.className = 'tree-node';
                nodeEl.style.paddingLeft = `${level * 20}px`;
                const contentEl = document.createElement('div');
                contentEl.className = 'tree-content';
                contentEl.dataset.id = folder.id;

                const children = appState.folderTreeData.filter(f => f.parent_id === folder.id);
                const hasChildren = children.length > 0;

                let toggleIcon = '';
                if (hasChildren) {
                    const isExpanded = expandedIds.has(folder.id);
                    toggleIcon = `<i class="fas ${isExpanded ? 'fa-caret-down' : 'fa-caret-right'} tree-toggle"></i>`;
                } else {
                    toggleIcon = `<span class="tree-toggle-placeholder"></span>`;
                }

                contentEl.innerHTML = `${toggleIcon} <i class="fas fa-folder"></i> <span class="folder-name">${folder.name}</span>`;

                if (folder.id === appState.currentFolderId) {
                    contentEl.classList.add('current-location');
                    contentEl.title = window.t('dialog.current_location');
                }

                contentEl.addEventListener('click', (e) => {
                    if (folder.id === appState.currentFolderId) return;

                    if (e.target.classList.contains('tree-toggle')) {
                        e.stopPropagation();
                        if (expandedIds.has(folder.id)) expandedIds.delete(folder.id);
                        else expandedIds.add(folder.id);
                        renderMoveTree(); 
                        return;
                    }
                    document.querySelectorAll('#move-tree-container .tree-content.selected').forEach(el => el.classList.remove('selected'));
                    contentEl.classList.add('selected');
                    selectedTargetId = folder.id;
                    confirmBtn.disabled = false;
                });
                nodeEl.appendChild(contentEl);

                if (hasChildren && expandedIds.has(folder.id)) {
                    const childrenContainer = document.createElement('div');
                    children.forEach(child => {
                        const childNode = createNode(child, level + 1);
                        if (childNode) childrenContainer.appendChild(childNode);
                    });
                    nodeEl.appendChild(childrenContainer);
                }

                return nodeEl;
            };

            roots.forEach(root => {
                const rootNode = createNode(root, 0);
                if (rootNode) treeContainer.appendChild(rootNode);
            });
        };

        renderMoveTree();
        confirmBtn.disabled = true; 
        uiManager.toggleModal(modalId, true);
        return new Promise(resolve => {
            const cleanup = () => {
                uiManager.toggleModal(modalId, false);
                confirmBtn.removeEventListener('click', onConfirm);
                cancelBtn.removeEventListener('click', onCancel);
                closeBtn.removeEventListener('click', onCancel);
            };

            const onConfirm = async () => {
                if (selectedTargetId === null) return;
                cleanup();
                await onConfirmCb(selectedTargetId);
                resolve();
            };
            const onCancel = () => {
                cleanup();
                resolve();
            };
            confirmBtn.addEventListener('click', onConfirm);
            cancelBtn.addEventListener('click', onCancel);
            closeBtn.addEventListener('click', onCancel);
        });
    },

    showDetailsModal(appState, uiModals) {
        const items = appState.selectedItems;
        if (items.length === 0) return;

        let contentHTML = '';
        if (items.length === 1) {
            const item = items[0];
            const sizeStr = item.type === 'file' ? (item.size || '--') : '--';
            const dateStr = item.modif_date || item.created_at || item.uploaded_at || '--';
            const iconClass = item.type === 'folder' ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
            
            let pathStr = '';
            let currentId = item.parent_id || appState.currentFolderId;
            const pathArr = [];
            while (currentId) {
                const folder = appState.folderMap.get(currentId);
                if (folder) {
                    pathArr.unshift(folder.name);
                    currentId = folder.parent_id;
                } else break;
            }
            if (pathArr.length > 0) pathStr = pathArr.join('/') + '/';
            else pathStr = '/';

            let typeStr = window.t('file_list.type_file');
            if (item.type === 'folder') {
                typeStr = window.t('file_list.type_folder');
            } else {
                const parts = item.name.split('.');
                if (parts.length > 1) {
                    const ext = parts.pop().toLowerCase();
                    const extKey = `file_list.type_${ext}`;
                    const translatedExt = window.t(extKey);
                    
                    if (translatedExt !== extKey) {
                        typeStr = translatedExt;
                    } else {
                        typeStr = ext.toUpperCase() + ' ' + window.t('file_list.type_file');
                    }
                }
            }

            let visualHtml = `<i class="${iconClass}" style="font-size: 56px; margin-bottom: 12px; color: var(--primary-color);"></i>`;
            if (item.type === 'file' && appState.currentThumbnails && appState.currentThumbnails[item.id]) {
                visualHtml = `<img src="data:image/jpeg;base64,${appState.currentThumbnails[item.id]}" style="max-width: 120px; max-height: 120px; border-radius: 8px; margin-bottom: 12px; object-fit: cover; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">`;
            }

            contentHTML = `
                <div style="text-align: center; margin-bottom: 20px;">
                    ${visualHtml}
                </div>
                <table style="width: 100%; text-align: left; border-spacing: 0 10px; font-size: 0.95rem;">
                    <tr><td style="color: #666; width: 90px;">${window.t('dialog.detail_name')}</td><td style="word-break: break-all;">${item.name}</td></tr>
                    <tr><td style="color: #666; width: 90px;">${window.t('dialog.detail_type')}</td><td>${typeStr}</td></tr>
                    <tr><td style="color: #666;">${window.t('dialog.detail_size')}</td><td>${sizeStr}</td></tr>
                    <tr><td style="color: #666;">${window.t('dialog.detail_date')}</td><td>${dateStr}</td></tr>
                    <tr><td style="color: #666;">${window.t('dialog.detail_path')}</td><td style="word-break: break-all;">${pathStr}</td></tr>
                </table>
                <div id="adv-details-container" style="margin-top: 15px; border-top: 1px solid var(--border-color); padding-top: 15px; min-height: 40px;">
                    <div id="adv-details-spinner" style="text-align: center; color: #999;">
                        <i class="fas fa-spinner fa-spin"></i> ${window.t('main.loading')}
                    </div>
                    <table id="adv-details-table" style="width: 100%; text-align: left; border-spacing: 0 10px; font-size: 0.95rem; display: none;">
                    </table>
                </div>
            `;

            uiModals.showAlert(window.t('dialog.details_title'), contentHTML, 'btn-primary');
            
            const titleEl = document.getElementById('alert-title');
            if (titleEl) {
                titleEl.removeAttribute('data-i18n');
                titleEl.textContent = window.t('dialog.details_title');
            }

            const spinner = document.getElementById('adv-details-spinner');
            if (item.type === 'folder') {
                spinner.style.display = 'none';
            } else {
                if (window.tdrive_bridge && window.tdrive_bridge.get_file_extended_details) {
                    window.tdrive_bridge.get_file_extended_details(item.id, (responseStr) => {
                        spinner.style.display = 'none';
                        try {
                            const response = JSON.parse(responseStr);
                            if (response && response.success && response.metadata && Object.keys(response.metadata).length > 0) {
                                const table = document.getElementById('adv-details-table');
                                let rows = '';
                                for (const [key, value] of Object.entries(response.metadata)) {
                                    const translatedKey = window.t(`meta.${key}`) || key;
                                    rows += `<tr><td style="color: #666; width: 90px;">${translatedKey}:</td><td style="word-break: break-all;">${value}</td></tr>`;
                                }
                                table.innerHTML = rows;
                                table.style.display = 'table';
                            }
                        } catch (e) {
                            console.error('Failed to parse extended details', e);
                        }
                    });
                } else {
                    spinner.style.display = 'none';
                }
            }
        } else {
            let totalSize = 0;
            let folderCount = 0;
            let fileCount = 0;

            items.forEach(item => {
                if (item.type === 'folder') {
                    folderCount++;
                } else {
                    fileCount++;
                    totalSize += (item.raw_size || 0);
                }
            });

            contentHTML = `
                <div style="text-align: center; margin-bottom: 20px;">
                    <i class="fas fa-layer-group" style="font-size: 56px; margin-bottom: 12px; color: var(--primary-color);"></i>
                    <h3 style="margin: 0;">${items.length} ${window.t('dialog.items_selected')}</h3>
                </div>
                <table style="width: 100%; text-align: left; border-spacing: 0 10px; font-size: 0.95rem;">
                    <tr><td style="color: #666; width: 90px;">${window.t('dialog.detail_folders')}</td><td>${folderCount}</td></tr>
                    <tr><td style="color: #666;">${window.t('dialog.detail_files')}</td><td>${fileCount}</td></tr>
                    <tr><td style="color: #666;">${window.t('dialog.detail_total_size')}</td><td>${UIManager.formatBytes(totalSize)}</td></tr>
                </table>
            `;
            
            uiModals.showAlert(window.t('dialog.details_title'), contentHTML, 'btn-primary');
        }
    }
};
