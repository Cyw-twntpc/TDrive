/**
 * Stage 3 Audio Studio window views: library/folder/playlist navigation,
 * sorting, chunked rendering, multi-selection, and drag-and-drop reordering.
 */
const AudioStudioView = {
    /** Refresh cloud audio library and local playlist summaries for Stage 3 studio. */
    async refreshStudioData() {
        try {
            const audioRes = await ApiService.getAllAudioFiles();
            if (audioRes && audioRes.success && Array.isArray(audioRes.items)) {
                AudioPlayerHandler._allAudioFiles = audioRes.items;
            }
        } catch (err) {
            console.error('Failed to load audio files:', err);
        }

        try {
            const plRes = await ApiService.getPlaylists();
            if (plRes && plRes.success && Array.isArray(plRes.playlists)) {
                AudioPlayerHandler._playlists = plRes.playlists;
            }
        } catch (err) {
            console.error('Failed to load playlists:', err);
        }

        if (AudioPlayerHandler._studioView !== 'all' && AudioPlayerHandler._studioView !== 'folders') {
            const exists = AudioPlayerHandler._playlists.some(p => p.id === AudioPlayerHandler._studioView);
            if (!exists) {
                AudioPlayerHandler._studioView = 'all';
                AudioPlayerHandler._activePlaylistDetail = null;
            } else {
                await AudioPlayerHandler._loadActivePlaylistDetail(AudioPlayerHandler._studioView);
            }
        }

        AudioPlayerHandler._renderStudioSidebar();
        AudioPlayerHandler._renderStudioTrackList();
    },

    /** Switch active view in Stage 3 studio ('all', 'folders', or a local playlist ID). */
    async selectStudioView(viewId) {
        AudioPlayerHandler._studioView = viewId;
        AudioPlayerHandler._selectedTrackIds.clear();
        AudioPlayerHandler._lastSelectedIndex = -1;
        AudioPlayerHandler._lastSelectedGroup = null;
        if (viewId !== 'all' && viewId !== 'folders') {
            AudioPlayerHandler._studioSort = { key: 'custom', order: 'asc' };
            await AudioPlayerHandler._loadActivePlaylistDetail(viewId);
        } else {
            if (AudioPlayerHandler._studioSort.key === 'custom') {
                AudioPlayerHandler._studioSort = { key: 'name', order: 'asc' };
            }
            AudioPlayerHandler._activePlaylistDetail = null;
        }
        AudioPlayerHandler._renderStudioSidebar();
        AudioPlayerHandler._renderStudioTrackList();
    },

    async _loadActivePlaylistDetail(playlistId) {
        try {
            const res = await ApiService.getPlaylistItems(playlistId);
            AudioPlayerHandler._activePlaylistDetail = (res && res.success && res.playlist) ? res.playlist : null;
        } catch (err) {
            console.error('Failed to load playlist detail:', err);
            AudioPlayerHandler._activePlaylistDetail = null;
        }
    },

    /** Build queue from active folder file list and begin playing clicked fileId. */
    async playFromFileList(fileId, fileName = '') {
        const folderFiles = (typeof AppState !== 'undefined' && AppState.currentFolderContents && Array.isArray(AppState.currentFolderContents.files))
            ? AppState.currentFolderContents.files
            : [];
        const audioQueue = folderFiles.filter(
            f => !f.isUploading && typeof FileListView !== 'undefined' && FileListView.isAudioFile(f.name)
        );

        let queue = audioQueue.length > 0 ? audioQueue : [{ id: fileId, name: fileName || 'Audio' }];
        let startIndex = queue.findIndex(f => Number(f.id) === Number(fileId));
        if (startIndex < 0) {
            queue = [{ id: fileId, name: fileName || 'Audio' }];
            startIndex = 0;
        }

        await AudioPlayerHandler.playTrackList(queue, startIndex, false);
    },

    _extractDraggedTrackIds(e) {
        const dragged = AudioPlayerHandler._draggingTrack;
        if (dragged && Array.isArray(dragged.ids) && dragged.ids.length > 0) {
            return dragged.ids.map(Number).filter(n => !Number.isNaN(n));
        }
        const rawText = e?.dataTransfer?.getData('text/plain');
        if (!rawText) return [];
        try {
            const parsed = JSON.parse(rawText);
            return (Array.isArray(parsed) ? parsed : [parsed]).map(Number).filter(n => !Number.isNaN(n));
        } catch (_err) {
            const single = Number(rawText);
            return !Number.isNaN(single) && single > 0 ? [single] : [];
        }
    },

    _bindPlaylistDropTarget(btnEl, playlistId) {
        btnEl.addEventListener('dragover', (e) => {
            if (!AudioPlayerHandler._draggingTrack) return;
            e.preventDefault();
            if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
            btnEl.classList.add('drop-target');
        });
        btnEl.addEventListener('dragleave', () => {
            btnEl.classList.remove('drop-target');
        });
        btnEl.addEventListener('drop', async (e) => {
            e.preventDefault();
            btnEl.classList.remove('drop-target');
            const draggedIds = AudioPlayerHandler._extractDraggedTrackIds(e);
            AudioPlayerHandler._draggingTrack = null;
            AudioPlayerHandler._clearRowDragIndicators();
            if (draggedIds.length === 0) return;
            const res = await ApiService.addToPlaylist(playlistId, draggedIds);
            if (res && res.success) {
                await AudioPlayerHandler.refreshStudioData();
            } else {
                UIManager.handleBackendError(res);
            }
        });
    },

    _renderStudioSidebar() {
        const allBadge = document.getElementById('audio-count-all');
        const foldersBadge = document.getElementById('audio-count-folders');
        if (allBadge) allBadge.textContent = String(AudioPlayerHandler._allAudioFiles.length);

        if (foldersBadge) {
            const uniqueFolders = new Set(AudioPlayerHandler._allAudioFiles.map(t => t.folder_path || 'TDrive'));
            foldersBadge.textContent = String(uniqueFolders.size);
        }

        document.getElementById('audio-nav-all')?.classList.toggle('active', AudioPlayerHandler._studioView === 'all');
        document.getElementById('audio-nav-folders')?.classList.toggle('active', AudioPlayerHandler._studioView === 'folders');

        const listEl = document.getElementById('audio-playlist-sidebar-list');
        if (!listEl) return;
        listEl.innerHTML = '';

        if (AudioPlayerHandler._playlists.length === 0) {
            const emptyLi = document.createElement('li');
            emptyLi.className = 'audio-sidebar-empty';
            emptyLi.textContent = AudioPlayerHandler._t('music.no_playlists', 'No local playlists');
            listEl.appendChild(emptyLi);
            return;
        }

        AudioPlayerHandler._playlists.forEach(pl => {
            const li = document.createElement('li');
            const btn = document.createElement('button');
            btn.className = 'audio-sidebar-item' + (AudioPlayerHandler._studioView === pl.id ? ' active' : '');
            btn.dataset.playlistId = pl.id;
            btn.innerHTML = `
                <span class="audio-sidebar-item-left">
                    <i class="fas fa-compact-disc"></i>
                    <span class="audio-playlist-name">${UIManager.escapeHtml(pl.name)}</span>
                </span>
                <span class="audio-count-badge">${pl.item_count}</span>
            `;
            btn.addEventListener('click', () => AudioPlayerHandler.selectStudioView(pl.id));
            AudioPlayerHandler._bindPlaylistDropTarget(btn, pl.id);
            li.appendChild(btn);
            listEl.appendChild(li);
        });
    },

    _extractTrackExt(fileName) {
        return (fileName && fileName.includes('.')) ? fileName.split('.').pop().toUpperCase() : 'AUDIO';
    },

    _updateSortHeaderUI() {
        const { key, order } = AudioPlayerHandler._studioSort;
        document.querySelectorAll('.audio-track-table-header .audio-sortable-col').forEach((col) => {
            col.classList.remove('asc', 'desc');
            if (col.dataset.sort === key) {
                col.classList.add(order === 'desc' ? 'desc' : 'asc');
            }
        });
    },

    _sortTracks(tracks) {
        const { key, order } = AudioPlayerHandler._studioSort;
        if (!key || key === 'custom') return tracks.slice();
        const dir = order === 'desc' ? -1 : 1;
        return tracks.slice().sort((a, b) => {
            let cmp = 0;
            if (key === 'size') {
                cmp = (Number(a.raw_size) || 0) - (Number(b.raw_size) || 0);
            } else if (key === 'folder') {
                cmp = (a.folder_path || 'TDrive').localeCompare(b.folder_path || 'TDrive', undefined, { sensitivity: 'base' });
            } else if (key === 'format') {
                cmp = AudioPlayerHandler._extractTrackExt(a.name).localeCompare(
                    AudioPlayerHandler._extractTrackExt(b.name),
                    undefined,
                    { sensitivity: 'base' }
                );
            } else {
                cmp = (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' });
            }
            if (cmp === 0) {
                cmp = (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' });
            }
            return cmp * dir;
        });
    },

    _getActiveViewTracks() {
        let baseTracks;
        if (AudioPlayerHandler._studioView === 'all' || AudioPlayerHandler._studioView === 'folders') {
            baseTracks = AudioPlayerHandler._allAudioFiles;
        } else {
            baseTracks = AudioPlayerHandler._activePlaylistDetail?.items || [];
        }

        const q = AudioPlayerHandler._searchQuery;
        const filtered = q
            ? baseTracks.filter(item =>
                (item.name || '').toLowerCase().includes(q)
                || (item.folder_path || '').toLowerCase().includes(q)
            )
            : baseTracks;
        return AudioPlayerHandler._sortTracks(filtered);
    },

    _renderStudioTrackList() {
        const listEl = document.getElementById('audio-studio-track-list');
        const titleEl = document.getElementById('audio-view-title');
        const countEl = document.getElementById('audio-view-count');
        const manageBtns = document.getElementById('audio-playlist-manage-btns');
        if (!listEl) return;

        AudioPlayerHandler._updateSortHeaderUI();

        const isCustomPlaylist = AudioPlayerHandler._studioView !== 'all' && AudioPlayerHandler._studioView !== 'folders';
        if (manageBtns) manageBtns.classList.toggle('hidden', !isCustomPlaylist);

        if (AudioPlayerHandler._studioView === 'all') {
            if (titleEl) titleEl.textContent = AudioPlayerHandler._t('music.all_music', 'All Music');
        } else if (AudioPlayerHandler._studioView === 'folders') {
            if (titleEl) titleEl.textContent = AudioPlayerHandler._t('music.by_folder', 'Browse by Folder');
        } else {
            const plName = AudioPlayerHandler._activePlaylistDetail?.name
                || AudioPlayerHandler._playlists.find(p => p.id === AudioPlayerHandler._studioView)?.name
                || '';
            if (titleEl) titleEl.textContent = plName;
        }

        const tracks = AudioPlayerHandler._getActiveViewTracks();
        if (countEl) {
            const tpl = AudioPlayerHandler._t('music.track_count_fmt', '{count} tracks');
            countEl.textContent = tpl.replace('{count}', String(tracks.length));
        }

        if (AudioPlayerHandler._trackRenderState.observer) {
            AudioPlayerHandler._trackRenderState.observer.disconnect();
            AudioPlayerHandler._trackRenderState.observer = null;
        }
        AudioPlayerHandler._trackRenderState.tracks = [];
        AudioPlayerHandler._trackRenderState.index = 0;

        listEl.innerHTML = '';
        if (tracks.length === 0) {
            const emptyDiv = document.createElement('div');
            emptyDiv.className = 'audio-empty-state';
            emptyDiv.innerHTML = `
                <i class="fas fa-music"></i>
                <p>${UIManager.escapeHtml(AudioPlayerHandler._t('music.empty_tracks', 'No audio tracks found'))}</p>
            `;
            listEl.appendChild(emptyDiv);
            return;
        }

        if (AudioPlayerHandler._studioView === 'folders') {
            AudioPlayerHandler._renderGroupedByFolder(listEl, tracks);
            return;
        }

        AudioPlayerHandler._trackRenderState.tracks = tracks;
        AudioPlayerHandler._trackRenderState.index = 0;
        AudioPlayerHandler._trackRenderState.isCustomPlaylist = isCustomPlaylist;
        AudioPlayerHandler._renderNextTrackChunk();
    },

    _renderNextTrackChunk() {
        const listEl = document.getElementById('audio-studio-track-list');
        const st = AudioPlayerHandler._trackRenderState;
        if (!listEl || st.index >= st.tracks.length) return;

        const oldSentinel = listEl.querySelector('.audio-scroll-sentinel');
        if (oldSentinel) oldSentinel.remove();

        const fragment = document.createDocumentFragment();
        const endIndex = Math.min(st.index + st.chunkSize, st.tracks.length);
        for (let i = st.index; i < endIndex; i++) {
            fragment.appendChild(
                AudioPlayerHandler._createTrackRowElement(st.tracks[i], i, st.tracks, st.isCustomPlaylist)
            );
        }
        st.index = endIndex;
        listEl.appendChild(fragment);

        if (st.index < st.tracks.length) {
            const sentinel = document.createElement('div');
            sentinel.className = 'audio-scroll-sentinel';
            listEl.appendChild(sentinel);

            if (!st.observer && typeof IntersectionObserver !== 'undefined') {
                st.observer = new IntersectionObserver((entries) => {
                    if (entries[0] && entries[0].isIntersecting) {
                        AudioPlayerHandler._renderNextTrackChunk();
                    }
                }, { root: listEl, rootMargin: '120px' });
            }
            if (st.observer) st.observer.observe(sentinel);
        }
    },

    _renderGroupedByFolder(listEl, tracks) {
        const groups = new Map();
        tracks.forEach(track => {
            const pathKey = track.folder_path || 'TDrive';
            if (!groups.has(pathKey)) groups.set(pathKey, []);
            groups.get(pathKey).push(track);
        });

        let runningIndex = 0;
        groups.forEach((groupTracks, folderPath) => {
            const groupHeader = document.createElement('div');
            groupHeader.className = 'audio-folder-group-header';
            groupHeader.innerHTML = `
                <div class="audio-folder-group-title">
                    <i class="fas fa-folder"></i>
                    <span>${UIManager.escapeHtml(folderPath)}</span>
                    <span class="audio-count-badge">${groupTracks.length}</span>
                </div>
                <button class="audio-btn-ghost sm audio-folder-play-btn" data-tooltip="${UIManager.escapeHtml(AudioPlayerHandler._t('music.play_all', 'Play All'))}">
                    <i class="fas fa-play"></i>
                </button>
            `;
            groupHeader.querySelector('.audio-folder-play-btn')?.addEventListener('click', () => {
                AudioPlayerHandler.playTrackList(groupTracks, 0, false);
            });
            listEl.appendChild(groupHeader);

            groupTracks.forEach((track, localIdx) => {
                listEl.appendChild(
                    AudioPlayerHandler._createTrackRowElement(track, localIdx, groupTracks, false, runningIndex + localIdx)
                );
            });
            runningIndex += groupTracks.length;
        });
    },

    _computeReorderedTrackIds(ids, draggedIdsOrId, targetId, insertAfter = false) {
        const list = (ids || []).map(Number);
        const dst = Number(targetId);
        const rawMoving = Array.isArray(draggedIdsOrId) ? draggedIdsOrId : [draggedIdsOrId];
        const movingSet = new Set(rawMoving.map(Number));
        const moving = list.filter(id => movingSet.has(id));
        if (moving.length === 0 || movingSet.has(dst) || !list.includes(dst)) return list;

        const remaining = list.filter(id => !movingSet.has(id));
        let dstIdx = remaining.indexOf(dst);
        if (insertAfter) dstIdx += 1;
        remaining.splice(dstIdx, 0, ...moving);
        return remaining;
    },

    _syncTrackRowSelectionDOM() {
        document.querySelectorAll('#audio-studio-track-list .audio-track-row').forEach((row) => {
            row.classList.toggle('selected', AudioPlayerHandler._selectedTrackIds.has(Number(row.dataset.id)));
        });
    },

    _handleTrackRowSelection(e, track, indexInGroup, groupList) {
        if (e.target && e.target.closest('button')) return;
        const idNum = Number(track.id);
        if (
            e.shiftKey
            && AudioPlayerHandler._lastSelectedIndex >= 0
            && AudioPlayerHandler._lastSelectedGroup === groupList
        ) {
            if (!e.ctrlKey && !e.metaKey) AudioPlayerHandler._selectedTrackIds.clear();
            const start = Math.min(AudioPlayerHandler._lastSelectedIndex, indexInGroup);
            const end = Math.max(AudioPlayerHandler._lastSelectedIndex, indexInGroup);
            for (let i = start; i <= end; i++) {
                const candidate = groupList[i];
                if (candidate && !candidate.missing) AudioPlayerHandler._selectedTrackIds.add(Number(candidate.id));
            }
        } else if (e.ctrlKey || e.metaKey) {
            if (AudioPlayerHandler._selectedTrackIds.has(idNum)) {
                AudioPlayerHandler._selectedTrackIds.delete(idNum);
            } else {
                AudioPlayerHandler._selectedTrackIds.add(idNum);
            }
            AudioPlayerHandler._lastSelectedIndex = indexInGroup;
            AudioPlayerHandler._lastSelectedGroup = groupList;
        } else {
            AudioPlayerHandler._selectedTrackIds.clear();
            AudioPlayerHandler._selectedTrackIds.add(idNum);
            AudioPlayerHandler._lastSelectedIndex = indexInGroup;
            AudioPlayerHandler._lastSelectedGroup = groupList;
        }
        AudioPlayerHandler._syncTrackRowSelectionDOM();
    },

    _clearRowDragIndicators() {
        document.querySelectorAll('.audio-track-row.dragging, .audio-track-row.drag-over-before, .audio-track-row.drag-over-after')
            .forEach(el => el.classList.remove('dragging', 'drag-over-before', 'drag-over-after'));
        document.querySelectorAll('.audio-sidebar-item.drop-target')
            .forEach(el => el.classList.remove('drop-target'));
    },

    _bindRowDragReorder(row, track) {
        row.addEventListener('dragover', (e) => {
            const dragged = AudioPlayerHandler._draggingTrack;
            const movingSet = new Set((dragged?.ids || [dragged?.id]).map(Number));
            if (!dragged || movingSet.has(Number(track.id))) return;
            e.preventDefault();
            if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
            const rect = row.getBoundingClientRect();
            const insertAfter = e.clientY > (rect.top + rect.height / 2);
            row.classList.toggle('drag-over-after', insertAfter);
            row.classList.toggle('drag-over-before', !insertAfter);
        });
        row.addEventListener('dragleave', () => {
            row.classList.remove('drag-over-before', 'drag-over-after');
        });
        row.addEventListener('drop', async (e) => {
            e.preventDefault();
            const insertAfter = row.classList.contains('drag-over-after');
            row.classList.remove('drag-over-before', 'drag-over-after');
            const detail = AudioPlayerHandler._activePlaylistDetail;
            const dragged = AudioPlayerHandler._draggingTrack;
            const movingIds = dragged ? (dragged.ids || [dragged.id]) : [];
            AudioPlayerHandler._draggingTrack = null;
            AudioPlayerHandler._clearRowDragIndicators();
            if (!detail || !Array.isArray(detail.items) || movingIds.length === 0) return;
            const orderedIds = AudioPlayerHandler._computeReorderedTrackIds(
                detail.items.map(i => i.id),
                movingIds,
                track.id,
                insertAfter
            );
            AudioPlayerHandler._studioSort = { key: 'custom', order: 'asc' };
            const res = await ApiService.reorderPlaylistItems(detail.id, orderedIds);
            if (!res || !res.success) {
                UIManager.handleBackendError(res);
                return;
            }
            await AudioPlayerHandler.refreshStudioData();
        });
    },

    _buildTrackRowMarkup(track, displayIdx, isCurrent, isCustomPlaylist) {
        const ext = AudioPlayerHandler._extractTrackExt(track.name);
        const folderLabel = track.missing
            ? AudioPlayerHandler._t('music.missing_track', 'File moved or in trash')
            : (track.folder_path || 'TDrive');

        const playBtnHtml = track.missing
            ? ''
            : '<button class="item-action-btn audio-row-play-btn"><i class="fas fa-play"></i></button>';
        const addBtnHtml = track.missing
            ? ''
            : `<button class="item-action-btn audio-row-add-btn" data-tooltip="${UIManager.escapeHtml(AudioPlayerHandler._t('music.add_to_playlist', 'Add to Playlist'))}"><i class="fas fa-plus-circle"></i></button>`;
        const removeBtnHtml = isCustomPlaylist
            ? `<button class="item-action-btn audio-remove-track-btn" data-tooltip="${UIManager.escapeHtml(AudioPlayerHandler._t('music.remove_from_playlist', 'Remove from playlist'))}"><i class="fas fa-times"></i></button>`
            : '';

        return `
            <span class="col-idx">
                ${isCurrent ? '<i class="fas fa-volume-up playing-indicator"></i>' : displayIdx}
            </span>
            <span class="col-name" data-tooltip="${UIManager.escapeHtml(track.name)}" data-tooltip-truncated="true">${UIManager.escapeHtml(track.name)}</span>
            <span class="col-folder">
                ${track.folder_id
                    ? `<button class="audio-folder-badge" data-tooltip="${UIManager.escapeHtml(folderLabel)}" data-tooltip-truncated="true"><i class="fas fa-folder"></i> ${UIManager.escapeHtml(folderLabel)}</button>`
                    : `<span class="audio-missing-badge">${UIManager.escapeHtml(folderLabel)}</span>`
                }
            </span>
            <span class="col-ext"><span class="audio-ext-pill">${UIManager.escapeHtml(ext)}</span></span>
            <span class="col-actions">${playBtnHtml}${addBtnHtml}${removeBtnHtml}</span>
        `;
    },

    _bindTrackRowEvents(row, track, indexInGroup, groupList, isCustomPlaylist) {
        if (!track.missing) {
            row.addEventListener('mousedown', (e) => {
                if (e.target && e.target.closest('button')) return;
                if (!e.ctrlKey && !e.metaKey && !e.shiftKey && !AudioPlayerHandler._selectedTrackIds.has(Number(track.id))) {
                    AudioPlayerHandler._selectedTrackIds.clear();
                    AudioPlayerHandler._selectedTrackIds.add(Number(track.id));
                    AudioPlayerHandler._lastSelectedIndex = indexInGroup;
                    AudioPlayerHandler._lastSelectedGroup = groupList;
                    AudioPlayerHandler._syncTrackRowSelectionDOM();
                }
            });
            row.addEventListener('click', (e) => AudioPlayerHandler._handleTrackRowSelection(e, track, indexInGroup, groupList));
            row.addEventListener('dblclick', () => AudioPlayerHandler.playTrackList(groupList, indexInGroup, false));
            row.addEventListener('dragstart', (e) => {
                const idNum = Number(track.id);
                const draggedIds = AudioPlayerHandler._selectedTrackIds.has(idNum) && AudioPlayerHandler._selectedTrackIds.size > 0
                    ? groupList.filter(t => !t.missing && AudioPlayerHandler._selectedTrackIds.has(Number(t.id))).map(t => Number(t.id))
                    : [idNum];
                AudioPlayerHandler._draggingTrack = {
                    id: idNum,
                    ids: draggedIds.length > 0 ? draggedIds : [idNum],
                    index: indexInGroup,
                    isCustomPlaylist,
                };
                const draggingSet = new Set(AudioPlayerHandler._draggingTrack.ids);
                document.querySelectorAll('#audio-studio-track-list .audio-track-row').forEach((el) => {
                    if (draggingSet.has(Number(el.dataset.id))) el.classList.add('dragging');
                });
                if (e.dataTransfer) {
                    e.dataTransfer.effectAllowed = isCustomPlaylist ? 'copyMove' : 'copy';
                    e.dataTransfer.setData('text/plain', JSON.stringify(AudioPlayerHandler._draggingTrack.ids));
                }
            });
            row.addEventListener('dragend', () => {
                AudioPlayerHandler._draggingTrack = null;
                AudioPlayerHandler._clearRowDragIndicators();
            });
            if (isCustomPlaylist) {
                AudioPlayerHandler._bindRowDragReorder(row, track);
            }
        }

        row.querySelector('.audio-row-play-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            AudioPlayerHandler.playTrackList(groupList, indexInGroup, false);
        });

        const folderBadge = row.querySelector('.audio-folder-badge');
        if (folderBadge && track.folder_id) {
            folderBadge.addEventListener('click', (e) => {
                e.stopPropagation();
                if (typeof window.switchPage === 'function') window.switchPage('files');
                if (typeof window.navigateTo === 'function') window.navigateTo(track.folder_id);
                AudioPlayerHandler.setMode(AudioPlayerHandler._currentTrack ? 'bar' : 'mini');
            });
        }

        row.querySelector('.audio-row-add-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            const idNum = Number(track.id);
            const useSelection = AudioPlayerHandler._selectedTrackIds.has(idNum) && AudioPlayerHandler._selectedTrackIds.size > 1;
            if (useSelection) {
                const selectedRows = groupList.filter(t => AudioPlayerHandler._selectedTrackIds.has(Number(t.id)));
                AudioPlayerHandler.openPlaylistPicker(selectedRows.map(t => t.id), selectedRows.map(t => t.name));
            } else {
                AudioPlayerHandler.openPlaylistPicker([track.id], [track.name]);
            }
        });

        row.querySelector('.audio-remove-track-btn')?.addEventListener('click', async (e) => {
            e.stopPropagation();
            const plId = AudioPlayerHandler._studioView;
            const res = await ApiService.removeFromPlaylist(plId, track.id);
            if (!res || !res.success) {
                UIManager.handleBackendError(res);
                return;
            }
            await AudioPlayerHandler.refreshStudioData();
        });
    },

    _createTrackRowElement(track, indexInGroup, groupList, isCustomPlaylist, displayIndexOverride = null) {
        const row = document.createElement('div');
        const activeId = AudioPlayerHandler._currentTrack ? Number(AudioPlayerHandler._currentTrack.id) : null;
        const isCurrent = activeId !== null && Number(track.id) === activeId;
        const isSelected = AudioPlayerHandler._selectedTrackIds.has(Number(track.id));
        row.className = 'audio-track-row'
            + (isCurrent ? ' playing' : '')
            + (isSelected ? ' selected' : '')
            + (track.missing ? ' missing' : '');
        row.dataset.id = String(track.id);
        row.draggable = !track.missing;

        const displayIdx = displayIndexOverride !== null ? displayIndexOverride + 1 : indexInGroup + 1;
        row.innerHTML = AudioPlayerHandler._buildTrackRowMarkup(track, displayIdx, isCurrent, isCustomPlaylist);
        AudioPlayerHandler._bindTrackRowEvents(row, track, indexInGroup, groupList, isCustomPlaylist);
        return row;
    },

    async playCurrentStudioView(shuffle = false) {
        const tracks = AudioPlayerHandler._getActiveViewTracks();
        await AudioPlayerHandler.playTrackList(tracks, 0, shuffle);
    },
};

Object.assign(AudioPlayerHandler, AudioStudioView);
