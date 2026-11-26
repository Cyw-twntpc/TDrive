const TrashView = {
    RENDER_CHUNK_SIZE: 80,
    listBody: document.getElementById('trash-list-body'),
    emptyBtn: document.getElementById('empty-trash-btn'),
    selectionBox: document.getElementById('trash-selection-box'),
    section: document.querySelector('#page-trash .file-list-section'),
    bulkActions: document.getElementById('trash-bulk-actions'),
    restoreBtn: document.getElementById('trash-restore-btn'),
    deleteBtn: document.getElementById('trash-delete-btn'),
    _thumbnails: new Map(),
    _renderState: {
        list: [],
        index: 0,
        chunkSize: 80,
        observer: null,
        scrollBound: false,
    },

    updateBulkActionState() {
        if (AppState.selectedItems.length > 0) {
            TrashView.bulkActions.classList.remove('hidden');
        } else {
            TrashView.bulkActions.classList.add('hidden');
        }
    },

    _ensureScrollListener() {
        if (TrashView._renderState.scrollBound || !TrashView.section || typeof TrashView.section.addEventListener !== 'function') {
            return;
        }
        TrashView._renderState.scrollBound = true;
        TrashView.section.addEventListener('scroll', () => {
            const container = TrashView.section;
            const state = TrashView._renderState;
            if (!state || state.index >= state.list.length) return;
            if (container.scrollTop + container.clientHeight >= container.scrollHeight - 240) {
                TrashView._renderNextChunk();
            }
        });
    },

    _createTrashItemElement(item) {
        const el = document.createElement('div');
        el.className = 'list-item trash-item';
        el.dataset.id = item.id;
        el.dataset.type = item.type;

        const iconClass = item.type === 'folder' ? 'fas fa-folder folder-icon' : UIManager.getFileTypeIcon(item.name);
        const pathName = UIManager.escapeHtml(item.displayPath || window.t('trash.unknown_location'));

        el.innerHTML = `
            <div class="trash-col-name">
                <div class="trash-col-main">
                    <i class="${iconClass} list-thumb-icon"></i>
                    <img class="list-thumb-img hidden" draggable="false" />
                    <span data-tooltip="${UIManager.escapeHtml(item.name)}" data-tooltip-truncated="true">${UIManager.escapeHtml(item.name)}</span>
                </div>
                <div class="trash-col-actions">
                    <button class="trash-action-btn restore-btn" data-tooltip="${window.t('trash.btn_restore')}"><i class="fas fa-undo-alt"></i></button>
                    <button class="trash-action-btn delete-btn" data-tooltip="${window.t('trash.btn_delete')}"><i class="fas fa-trash-alt"></i></button>
                </div>
            </div>
            <div>${item.trashed_date}</div>
            <div>${item.type === 'folder' ? window.t('file_list.type_folder') : window.t('file_list.type_file')}</div>
            <div class="trash-col-path" data-tooltip="${pathName}" data-tooltip-truncated="true">${pathName}</div> 
            <div>${item.size}</div>
        `;

        if (item.type === 'file' && TrashView._thumbnails instanceof Map) {
            const cachedB64 = TrashView._thumbnails.get(String(item.id));
            if (cachedB64) {
                const img = el.querySelector('.list-thumb-img');
                const icon = el.querySelector('.list-thumb-icon');
                if (img) {
                    img.src = `data:image/jpeg;base64,${cachedB64}`;
                    img.classList.remove('hidden');
                }
                if (icon) icon.classList.add('hidden');
            }
        }

        el.querySelector('.restore-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            TrashModel.restoreItem(item);
        });
        el.querySelector('.delete-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            TrashModel.deleteItemPermanently(item);
        });

        TrashHandler._addSelectionListener(el, item);
        return el;
    },

    _renderNextChunk() {
        const state = TrashView._renderState;
        if (!TrashView.listBody || state.index >= state.list.length) return;

        const oldSentinel = TrashView.listBody.querySelector('.scroll-sentinel');
        if (oldSentinel) oldSentinel.remove();

        const chunkSize = state.chunkSize || TrashView.RENDER_CHUNK_SIZE;
        const endIndex = Math.min(state.index + chunkSize, state.list.length);
        const fragment = document.createDocumentFragment();
        for (let i = state.index; i < endIndex; i++) {
            fragment.appendChild(TrashView._createTrashItemElement(state.list[i]));
        }
        state.index = endIndex;
        TrashView.listBody.appendChild(fragment);

        if (state.index < state.list.length && typeof IntersectionObserver !== 'undefined') {
            const sentinel = document.createElement('div');
            sentinel.className = 'scroll-sentinel';
            sentinel.style.height = '10px';
            TrashView.listBody.appendChild(sentinel);

            if (!state.observer) {
                state.observer = new IntersectionObserver((entries) => {
                    if (entries[0] && entries[0].isIntersecting) {
                        TrashView._renderNextChunk();
                    }
                }, { root: TrashView.section || null, rootMargin: '120px' });
            }
            state.observer.observe(sentinel);
        }
    },

    render() {
        TrashView.listBody.innerHTML = '';
        AppState.selectedItems.length = 0; // Reset selection on render
        TrashView.updateBulkActionState();

        if (TrashView._renderState.observer) {
            TrashView._renderState.observer.disconnect();
            TrashView._renderState.observer = null;
        }
        
        if (AppState.trashItems.length === 0) {
            TrashView._renderState.list = [];
            TrashView._renderState.index = 0;
            TrashView.listBody.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-trash-alt"></i>
                    <p>${window.t('trash.empty_text')}</p>
                </div>`;
            TrashView.emptyBtn.disabled = true;
            return;
        }
        
        TrashView.emptyBtn.disabled = false;
        TrashView._ensureScrollListener();
        TrashView._renderState.list = AppState.trashItems;
        TrashView._renderState.index = 0;
        TrashView._renderState.chunkSize = TrashView.RENDER_CHUNK_SIZE;
        TrashView._renderNextChunk();
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
