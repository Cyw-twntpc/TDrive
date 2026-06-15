const TrashView = {
    listBody: document.getElementById('trash-list-body'),
    emptyBtn: document.getElementById('empty-trash-btn'),
    selectionBox: document.getElementById('trash-selection-box'),
    section: document.querySelector('#page-trash .file-list-section'),
    bulkActions: document.getElementById('trash-bulk-actions'),
    restoreBtn: document.getElementById('trash-restore-btn'),
    deleteBtn: document.getElementById('trash-delete-btn'),

    updateBulkActionState() {
        if (AppState.selectedItems.length > 0) {
            TrashView.bulkActions.classList.remove('hidden');
        } else {
            TrashView.bulkActions.classList.add('hidden');
        }
    },

    render() {
        TrashView.listBody.innerHTML = '';
        AppState.selectedItems.length = 0; // Reset selection on render
        TrashView.updateBulkActionState();
        
        if (AppState.trashItems.length === 0) {
            TrashView.listBody.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-trash-alt"></i>
                    <p>${window.t('trash.empty_text')}</p>
                </div>`;
            TrashView.emptyBtn.disabled = true;
            return;
        }
        
        TrashView.emptyBtn.disabled = false;

        const fragment = document.createDocumentFragment();
        
        AppState.trashItems.forEach(item => {
            const el = document.createElement('div');
            el.className = 'trash-item';
            el.dataset.id = item.id;
            el.dataset.type = item.type;
            
            const iconClass = item.type === 'folder' ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
            
            const pathName = item.displayPath || window.t('trash.unknown_location');
            
            el.innerHTML = `
                <div class="trash-col-name">
                    <div class="trash-col-main">
                        <i class="${iconClass} list-thumb-icon"></i>
                        <img class="list-thumb-img hidden" draggable="false" />
                        <span title="${item.name}">${item.name}</span>
                    </div>
                    <div class="trash-col-actions">
                        <button class="trash-action-btn restore-btn" title="${window.t('trash.btn_restore')}"><i class="fas fa-undo-alt"></i></button>
                        <button class="trash-action-btn delete-btn" title="${window.t('trash.btn_delete')}"><i class="fas fa-trash-alt"></i></button>
                    </div>
                </div>
                <div>${item.trashed_date}</div>
                <div>${item.type === 'folder' ? window.t('file_list.type_folder') : window.t('file_list.type_file')}</div>
                <div class="trash-col-path" title="${pathName}">${pathName}</div> 
                <div>${item.size}</div>
            `;
            
            // Bind actions
            el.querySelector('.restore-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                TrashModel.restoreItem(item);
            });
            el.querySelector('.delete-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                TrashModel.deleteItemPermanently(item);
            });

            TrashHandler._addSelectionListener(el, item);
            fragment.appendChild(el);
        });
        
        TrashView.listBody.appendChild(fragment);
    },

    _getOriginalPath(folderId) {
        if (!folderId) return window.t('transfer.path_not_exists');
        if (!AppState || !AppState.folderMap) return window.t('transfer.path_not_exists');
        
        const path = [];
        let current = AppState.folderMap.get(folderId);

        // Case 1: ID not found in current map
        if (!current) return window.t('transfer.path_not_exists');

        while (current) {
            path.unshift(current.name);
            
            // Reached Root
            if (current.parent_id === null) {
                return path.join(' / ');
            }

            // Move up
            const next = AppState.folderMap.get(current.parent_id);
            
            // Case 2: Broken chain
            if (!next) return window.t('transfer.path_not_exists');
            
            current = next;
        }

        return path.join(' / ');
    },

};
