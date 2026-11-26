/**
 * Local playlist management dialogs and quick Add-to-Playlist picker modal.
 */
const AudioPlaylistPicker = {
    async promptCreatePlaylist() {
        const name = await UIModals.showPrompt(
            AudioPlayerHandler._t('music.new_playlist', 'New Playlist'),
            AudioPlayerHandler._t('music.enter_playlist_name', 'Enter playlist name:'),
            ''
        );
        if (!name || !name.trim()) return;
        const res = await ApiService.createPlaylist(name.trim());
        if (!res || !res.success) {
            UIManager.handleBackendError(res);
            return;
        }
        await AudioPlayerHandler.refreshStudioData();
        if (res.playlist?.id) {
            await AudioPlayerHandler.selectStudioView(res.playlist.id);
        }
    },

    async promptRenameActivePlaylist() {
        const pl = AudioPlayerHandler._activePlaylistDetail;
        if (!pl) return;
        const newName = await UIModals.showPrompt(
            AudioPlayerHandler._t('music.rename_playlist', 'Rename Playlist'),
            AudioPlayerHandler._t('music.enter_playlist_name', 'Enter playlist name:'),
            pl.name
        );
        if (!newName || !newName.trim() || newName.trim() === pl.name) return;
        const res = await ApiService.renamePlaylist(pl.id, newName.trim());
        if (!res || !res.success) {
            UIManager.handleBackendError(res);
            return;
        }
        await AudioPlayerHandler.refreshStudioData();
    },

    async confirmDeleteActivePlaylist() {
        const pl = AudioPlayerHandler._activePlaylistDetail;
        if (!pl) return;
        const msg = AudioPlayerHandler._t('music.confirm_delete_playlist', 'Delete playlist "{name}"?')
            .replace('{name}', UIManager.escapeHtml(pl.name));
        const confirmed = await UIModals.showConfirm(
            AudioPlayerHandler._t('music.delete_playlist', 'Delete Playlist'),
            msg
        );
        if (!confirmed) return;
        const res = await ApiService.deletePlaylist(pl.id);
        if (!res || !res.success) {
            UIManager.handleBackendError(res);
            return;
        }
        AudioPlayerHandler._studioView = 'all';
        AudioPlayerHandler._activePlaylistDetail = null;
        await AudioPlayerHandler.refreshStudioData();
    },

    /** Open quick Add-to-Playlist modal from file list rows, floating toolbar, or studio rows. */
    async openPlaylistPicker(mapIds, fileNames = []) {
        const cleanIds = (mapIds || []).filter(id => id !== undefined && id !== null);
        if (cleanIds.length === 0) return;

        AudioPlayerHandler._pickerIds = cleanIds;
        AudioPlayerHandler._pickerNames = fileNames;

        const targetInfoEl = document.getElementById('playlist-picker-target-info');
        if (targetInfoEl) {
            if (cleanIds.length === 1 && fileNames[0]) {
                targetInfoEl.textContent = AudioPlayerHandler._t('music.picker_single_target', 'Add "{name}" to playlist:')
                    .replace('{name}', fileNames[0]);
            } else {
                targetInfoEl.textContent = AudioPlayerHandler._t('music.picker_multi_target', 'Add {count} audio files to playlist:')
                    .replace('{count}', String(cleanIds.length));
            }
        }

        const newNameInput = document.getElementById('playlist-picker-new-name');
        if (newNameInput) newNameInput.value = '';

        try {
            const res = await ApiService.getPlaylists();
            if (res && res.success && Array.isArray(res.playlists)) {
                AudioPlayerHandler._playlists = res.playlists;
            }
        } catch (err) {
            console.error('Failed to load playlists for picker:', err);
        }

        AudioPlayerHandler._renderPlaylistPickerList();
        UIManager.toggleModal('playlist-picker-modal', true);
    },

    _renderPlaylistPickerList() {
        const listEl = document.getElementById('playlist-picker-list');
        if (!listEl) return;
        listEl.innerHTML = '';

        if (AudioPlayerHandler._playlists.length === 0) {
            const emptyP = document.createElement('p');
            emptyP.className = 'playlist-picker-empty';
            emptyP.textContent = AudioPlayerHandler._t('music.no_playlists_hint', 'No playlists yet. Create one below!');
            listEl.appendChild(emptyP);
            return;
        }

        AudioPlayerHandler._playlists.forEach(pl => {
            const itemBtn = document.createElement('button');
            itemBtn.className = 'playlist-picker-item';
            const countLabel = AudioPlayerHandler._t('music.track_count_fmt', '{count} tracks')
                .replace('{count}', String(pl.item_count));
            itemBtn.innerHTML = `
                <span class="playlist-picker-item-name"><i class="fas fa-compact-disc"></i> ${UIManager.escapeHtml(pl.name)}</span>
                <span class="audio-count-badge">${UIManager.escapeHtml(countLabel)}</span>
            `;
            itemBtn.addEventListener('click', async () => {
                const res = await ApiService.addToPlaylist(pl.id, AudioPlayerHandler._pickerIds);
                if (!res || !res.success) {
                    UIManager.handleBackendError(res);
                    return;
                }
                UIManager.toggleModal('playlist-picker-modal', false);
                if (AudioPlayerHandler._mode === 'window') {
                    await AudioPlayerHandler.refreshStudioData();
                } else {
                    const plRes = await ApiService.getPlaylists();
                    if (plRes && plRes.success) AudioPlayerHandler._playlists = plRes.playlists;
                }
            });
            listEl.appendChild(itemBtn);
        });
    },

    async _handleCreateAndAddFromPicker() {
        const input = document.getElementById('playlist-picker-new-name');
        const name = (input?.value || '').trim();
        if (!name) return;

        const createRes = await ApiService.createPlaylist(name);
        if (!createRes || !createRes.success || !createRes.playlist) {
            UIManager.handleBackendError(createRes);
            return;
        }

        const addRes = await ApiService.addToPlaylist(createRes.playlist.id, AudioPlayerHandler._pickerIds);
        if (!addRes || !addRes.success) {
            UIManager.handleBackendError(addRes);
            return;
        }

        UIManager.toggleModal('playlist-picker-modal', false);
        await AudioPlayerHandler.refreshStudioData();
    },
};

Object.assign(AudioPlayerHandler, AudioPlaylistPicker);
