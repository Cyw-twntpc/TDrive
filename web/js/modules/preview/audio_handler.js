/**
 * Manages the 3-stage progressive Audio Studio (mini -> bar -> window),
 * local playlists, all-cloud audio library view, and file-list quick-add workflows.
 */
const AudioPlayerHandler = {
    _initialized: false,
    _domBound: false,
    _signalBound: false,
    _mode: 'mini',
    _queue: [],
    _currentIndex: -1,
    _currentTrack: null,
    _playbackState: 'stopped',
    _currentMs: 0,
    _durationMs: 0,
    _isSeeking: false,
    _seekGuardTargetMs: null,
    _seekGuardUntil: 0,
    SEEK_GUARD_MS: 1500,
    _loopMode: 'off', // 'off' | 'loop-all' | 'loop-one' | 'shuffle'
    _volume: 80,
    _muted: false,
    _isAdjustingVolume: false,
    _volumeGuardUntil: 0,
    VOLUME_GUARD_MS: 300,
    _lastEndedTriggerKey: null,
    _lastErrorTriggerKey: null,
    _lastRenderedQueueKey: null,
    _playRequestSeq: 0,
    _shuffleOrder: [],
    _shufflePos: -1,
    _playHistory: [],

    _studioView: 'all', // 'all' | 'folders' | '<playlist_id>'
    _studioSort: { key: 'name', order: 'asc' },
    _searchQuery: '',
    _allAudioFiles: [],
    _playlists: [],
    _activePlaylistDetail: null,
    _pickerIds: [],
    _pickerNames: [],
    _draggingTrack: null,
    _selectedTrackIds: new Set(),
    _lastSelectedIndex: -1,
    _lastSelectedGroup: null,
    _barQueueCollapsed: false,
    _trackRenderState: {
        tracks: [],
        index: 0,
        chunkSize: 50,
        isCustomPlaylist: false,
        observer: null,
    },
    BAR_IDLE_TIMEOUT_MS: 15000,
    VOLUME_SYNC_DELAY_MS: 80,
    _barIdleTimer: null,
    _volumeSyncTimer: null,

    /** Safely translate an i18n key with a sensible fallback. */
    _t(key, fallback = '') {
        if (typeof window.t === 'function') {
            const val = window.t(key);
            if (val && val !== key) return val;
        }
        return fallback;
    },

    /** Format milliseconds into mm:ss or h:mm:ss display string. */
    formatTime(ms) {
        const totalSec = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
        const hours = Math.floor(totalSec / 3600);
        const mins = Math.floor((totalSec % 3600) / 60);
        const secs = totalSec % 60;
        const pad = (n) => String(n).padStart(2, '0');
        return hours > 0 ? `${hours}:${pad(mins)}:${pad(secs)}` : `${pad(mins)}:${pad(secs)}`;
    },

    /** Initialize DOM bindings, persisted volume, and QWebChannel signals. */
    init() {
        if (AudioPlayerHandler._initialized) return;
        AudioPlayerHandler._initialized = true;

        try {
            const savedVol = localStorage.getItem('tdrive_audio_volume');
            if (savedVol !== null && !Number.isNaN(Number(savedVol))) {
                AudioPlayerHandler._volume = Math.max(0, Math.min(100, parseInt(savedVol, 10)));
            }
            const savedMuted = localStorage.getItem('tdrive_audio_muted');
            if (savedMuted === '1' || savedMuted === 'true') {
                AudioPlayerHandler._muted = true;
            } else if (savedMuted === '0' || savedMuted === 'false') {
                AudioPlayerHandler._muted = false;
            }
            const savedLoop = localStorage.getItem('tdrive_audio_loop_mode');
            if (savedLoop === 'off' || savedLoop === 'loop-all' || savedLoop === 'loop-one' || savedLoop === 'shuffle') {
                AudioPlayerHandler._loopMode = savedLoop;
            }
        } catch (_err) {
            /* Ignore storage errors in restricted environments */
        }

        const volSlider = document.getElementById('audio-volume-slider');
        if (volSlider) {
            volSlider.value = String(AudioPlayerHandler._volume);
        }

        AudioPlayerHandler._bindDomEvents();
        AudioPlayerHandler._bindBridgeSignal();
        document.addEventListener('TDriveBridgeReady', () => AudioPlayerHandler._bindBridgeSignal());
        document.addEventListener('i18nChanged', () => AudioPlayerHandler._refreshDynamicLabels());

        AudioPlayerHandler.setMode('mini', { skipLoad: true });
        AudioPlayerHandler._renderTransportUI();
    },

    /** Connect to QWebChannel audio_state_changed signal once available. */
    _bindBridgeSignal() {
        if (AudioPlayerHandler._signalBound) return;
        const bridge = window.tdrive_bridge;
        if (bridge && bridge.audio_state_changed && typeof bridge.audio_state_changed.connect === 'function') {
            AudioPlayerHandler._signalBound = true;
            bridge.audio_state_changed.connect((rawState) => {
                try {
                    const parsed = typeof rawState === 'string' ? JSON.parse(rawState) : rawState;
                    AudioPlayerHandler.handleBackendState(parsed);
                } catch (err) {
                    console.error('Failed to parse audio_state_changed payload:', err);
                }
            });
            ApiService.controlAudio('volume', AudioPlayerHandler._volume).catch(() => {});
            ApiService.controlAudio('mute', AudioPlayerHandler._muted ? 1 : 0).catch(() => {});
        }
    },

    /** Bind click, slider, search, and modal listeners for the 3-stage Audio Studio. */
    _bindDomEvents() {
        if (AudioPlayerHandler._domBound) return;
        AudioPlayerHandler._domBound = true;
        // Stage transitions
        document.getElementById('audio-mini-expand-bar-btn')?.addEventListener('click', () => {
            AudioPlayerHandler.setMode('bar');
        });
        document.getElementById('audio-mini-expand-window-btn')?.addEventListener('click', () => {
            AudioPlayerHandler.setMode('window');
        });
        document.getElementById('audio-bar-open-window-btn')?.addEventListener('click', () => {
            AudioPlayerHandler.setMode('window');
        });
        document.getElementById('audio-window-to-bar-btn')?.addEventListener('click', () => {
            AudioPlayerHandler.setMode('bar');
        });
        document.getElementById('audio-window-to-mini-btn')?.addEventListener('click', () => {
            AudioPlayerHandler.setMode('mini');
        });
        document.getElementById('audio-studio-backdrop')?.addEventListener('click', () => {
            AudioPlayerHandler.setMode(AudioPlayerHandler._currentTrack ? 'bar' : 'mini');
        });

        // Auto-minimize bar mode on outside click or inactivity timeout
        const studioEl = document.getElementById('audio-studio');
        if (studioEl) {
            ['mousemove', 'mousedown', 'click', 'input', 'wheel', 'keydown', 'touchstart'].forEach((evtName) => {
                studioEl.addEventListener(evtName, () => {
                    if (AudioPlayerHandler._mode === 'bar') {
                        AudioPlayerHandler._resetBarIdleTimer();
                    }
                }, { passive: true });
            });
        }
        document.addEventListener('mousedown', (e) => {
            if (AudioPlayerHandler._mode !== 'bar') return;
            const target = e.target;
            if (target && typeof target.closest === 'function') {
                if (target.closest('#audio-studio') || target.closest('.modal-overlay:not(.hidden)')) {
                    return;
                }
            }
            AudioPlayerHandler.setMode('mini', { skipLoad: true });
        });

        // Sortable studio columns
        document.querySelectorAll('.audio-track-table-header .audio-sortable-col').forEach((headerCol) => {
            headerCol.addEventListener('click', () => {
                const sortKey = headerCol.dataset.sort;
                if (!sortKey) return;
                if (AudioPlayerHandler._studioSort.key === sortKey) {
                    AudioPlayerHandler._studioSort.order = AudioPlayerHandler._studioSort.order === 'asc' ? 'desc' : 'asc';
                } else {
                    AudioPlayerHandler._studioSort = { key: sortKey, order: 'asc' };
                }
                AudioPlayerHandler._renderStudioTrackList();
            });
        });

        // Bar queue drawer collapse/expand toggle
        document.getElementById('audio-bar-queue-header')?.addEventListener('click', () => {
            AudioPlayerHandler.toggleBarQueue();
        });
        document.getElementById('audio-bar-queue-toggle-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            AudioPlayerHandler.toggleBarQueue();
        });

        // Infinite scroll fallback for studio track list
        const studioTrackListEl = document.getElementById('audio-studio-track-list');
        if (studioTrackListEl) {
            studioTrackListEl.addEventListener('scroll', () => {
                const st = AudioPlayerHandler._trackRenderState;
                if (st.index < st.tracks.length
                    && studioTrackListEl.scrollTop + studioTrackListEl.clientHeight >= studioTrackListEl.scrollHeight - 120) {
                    AudioPlayerHandler._renderNextTrackChunk();
                }
            });
        }

        // Mini quick play/pause
        document.getElementById('audio-mini-play-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            if (!AudioPlayerHandler._currentTrack) {
                AudioPlayerHandler.setMode('window');
                return;
            }
            AudioPlayerHandler.togglePlay();
        });

        // Main transport controls
        document.getElementById('audio-play-pause-btn')?.addEventListener('click', () => AudioPlayerHandler.togglePlay());
        document.getElementById('audio-prev-btn')?.addEventListener('click', () => AudioPlayerHandler.playPrev());
        document.getElementById('audio-next-btn')?.addEventListener('click', () => AudioPlayerHandler.playNext(false));
        document.getElementById('audio-stop-btn')?.addEventListener('click', () => AudioPlayerHandler.stopPlayback());
        document.getElementById('audio-loop-btn')?.addEventListener('click', () => AudioPlayerHandler.cycleLoopMode());
        document.getElementById('audio-mute-btn')?.addEventListener('click', () => AudioPlayerHandler.toggleMute());

        // Seek slider interaction
        const seekSlider = document.getElementById('audio-seek-slider');
        if (seekSlider) {
            // Reset _isSeeking even when clicking the thumb without changing its value.
            const finishSeek = () => {
                if (!AudioPlayerHandler._isSeeking) return;
                AudioPlayerHandler._isSeeking = false;
                if (AudioPlayerHandler._mode === 'bar') {
                    AudioPlayerHandler._resetBarIdleTimer();
                }
                setTimeout(() => {
                    if (!AudioPlayerHandler._isSeeking) {
                        AudioPlayerHandler._renderProgressOnly();
                    }
                }, 0);
            };
            seekSlider.addEventListener('mousedown', () => { AudioPlayerHandler._isSeeking = true; });
            seekSlider.addEventListener('touchstart', () => { AudioPlayerHandler._isSeeking = true; }, { passive: true });
            window.addEventListener('mouseup', finishSeek);
            window.addEventListener('touchend', finishSeek);
            window.addEventListener('touchcancel', finishSeek);
            seekSlider.addEventListener('blur', finishSeek);
            seekSlider.addEventListener('input', () => {
                if (AudioPlayerHandler._durationMs > 0) {
                    const previewMs = Math.round((Number(seekSlider.value) / 1000) * AudioPlayerHandler._durationMs);
                    const curEl = document.getElementById('audio-time-current');
                    if (curEl) curEl.textContent = AudioPlayerHandler.formatTime(previewMs);
                }
            });
            seekSlider.addEventListener('change', () => {
                AudioPlayerHandler._isSeeking = false;
                if (AudioPlayerHandler._durationMs > 0) {
                    const targetMs = Math.round((Number(seekSlider.value) / 1000) * AudioPlayerHandler._durationMs);
                    AudioPlayerHandler.seekTo(targetMs);
                }
            });
        }

        // Volume slider interaction
        const volSlider = document.getElementById('audio-volume-slider');
        if (volSlider) {
            const endVolumeAdjust = () => {
                AudioPlayerHandler._isAdjustingVolume = false;
            };
            volSlider.addEventListener('mousedown', () => { AudioPlayerHandler._isAdjustingVolume = true; });
            volSlider.addEventListener('touchstart', () => { AudioPlayerHandler._isAdjustingVolume = true; }, { passive: true });
            window.addEventListener('mouseup', endVolumeAdjust);
            window.addEventListener('touchend', endVolumeAdjust);
            window.addEventListener('touchcancel', endVolumeAdjust);
            volSlider.addEventListener('blur', endVolumeAdjust);
            volSlider.addEventListener('input', () => {
                AudioPlayerHandler.setVolume(Number(volSlider.value));
            });
            volSlider.addEventListener('change', () => {
                AudioPlayerHandler._isAdjustingVolume = false;
                AudioPlayerHandler.setVolume(Number(volSlider.value), { immediate: true });
            });
        }

        // Global keyboard shortcuts when Audio Bar/Window is active without text input or modal focus
        document.addEventListener('keydown', (e) => {
            if (e.ctrlKey || e.metaKey || e.altKey) return;
            const target = e.target;
            const tagName = (target && target.tagName) ? target.tagName.toUpperCase() : '';
            if (tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT' || (target && target.isContentEditable)) {
                return;
            }
            if (document.querySelector('.modal-overlay:not(.hidden)')) {
                return;
            }

            const mode = AudioPlayerHandler._mode;
            if (e.key === 'Escape') {
                if (mode === 'window') {
                    e.preventDefault();
                    AudioPlayerHandler.setMode(AudioPlayerHandler._currentTrack ? 'bar' : 'mini');
                } else if (mode === 'bar') {
                    e.preventDefault();
                    AudioPlayerHandler.setMode('mini', { skipLoad: true });
                }
                return;
            }

            if ((mode !== 'bar' && mode !== 'window') || !AudioPlayerHandler._currentTrack) {
                return;
            }
            if (e.code === 'Space' || e.key === ' ') {
                e.preventDefault();
                AudioPlayerHandler.togglePlay();
            } else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && AudioPlayerHandler._durationMs > 0) {
                e.preventDefault();
                const delta = e.key === 'ArrowRight' ? 5000 : -5000;
                const targetMs = Math.max(
                    0,
                    Math.min(AudioPlayerHandler._durationMs, AudioPlayerHandler._currentMs + delta)
                );
                AudioPlayerHandler.seekTo(targetMs);
            }
        });

        // Studio navigation & search
        document.getElementById('audio-nav-all')?.addEventListener('click', () => AudioPlayerHandler.selectStudioView('all'));
        document.getElementById('audio-nav-folders')?.addEventListener('click', () => AudioPlayerHandler.selectStudioView('folders'));
        document.getElementById('audio-new-playlist-btn')?.addEventListener('click', () => AudioPlayerHandler.promptCreatePlaylist());
        document.getElementById('audio-playlist-rename-btn')?.addEventListener('click', () => AudioPlayerHandler.promptRenameActivePlaylist());
        document.getElementById('audio-playlist-delete-btn')?.addEventListener('click', () => AudioPlayerHandler.confirmDeleteActivePlaylist());
        document.getElementById('audio-play-all-btn')?.addEventListener('click', () => AudioPlayerHandler.playCurrentStudioView(false));
        document.getElementById('audio-shuffle-all-btn')?.addEventListener('click', () => AudioPlayerHandler.playCurrentStudioView(true));

        document.getElementById('audio-studio-search-input')?.addEventListener('input', (e) => {
            AudioPlayerHandler._searchQuery = (e.target.value || '').trim().toLowerCase();
            AudioPlayerHandler._renderStudioTrackList();
        });

        // Playlist picker modal listeners
        document.getElementById('playlist-picker-close-btn')?.addEventListener('click', () => UIManager.toggleModal('playlist-picker-modal', false));
        document.getElementById('playlist-picker-cancel-btn')?.addEventListener('click', () => UIManager.toggleModal('playlist-picker-modal', false));
        document.getElementById('playlist-picker-create-btn')?.addEventListener('click', () => AudioPlayerHandler._handleCreateAndAddFromPicker());
        document.getElementById('playlist-picker-new-name')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                AudioPlayerHandler._handleCreateAndAddFromPicker();
            }
        });
    },

    _clearBarIdleTimer() {
        if (AudioPlayerHandler._barIdleTimer) {
            clearTimeout(AudioPlayerHandler._barIdleTimer);
            AudioPlayerHandler._barIdleTimer = null;
        }
    },

    _resetBarIdleTimer() {
        AudioPlayerHandler._clearBarIdleTimer();
        if (AudioPlayerHandler._mode !== 'bar') return;
        AudioPlayerHandler._barIdleTimer = setTimeout(() => {
            if (AudioPlayerHandler._mode === 'bar' && !AudioPlayerHandler._isSeeking) {
                AudioPlayerHandler.setMode('mini', { skipLoad: true });
            }
        }, AudioPlayerHandler.BAR_IDLE_TIMEOUT_MS);
    },

    /** Hide mini indicator on non-Files views whenever no active track or queue exists. */
    updateVisibilityForPage(pageId = null) {
        const studioEl = document.getElementById('audio-studio');
        if (!studioEl) return;
        const currentPage = pageId || (typeof AppState !== 'undefined' && AppState.currentPage) || 'files';
        const hasQueue = AudioPlayerHandler._queue.length > 0 || Boolean(AudioPlayerHandler._currentTrack);
        const shouldHideMini = AudioPlayerHandler._mode === 'mini' && !hasQueue && currentPage !== 'files';
        studioEl.classList.toggle('page-hidden', shouldHideMini);
    },

    /** Switch the single #audio-studio container between 'mini', 'bar', and 'window'. */
    setMode(mode, options = {}) {
        const validMode = (mode === 'window' || mode === 'bar') ? mode : 'mini';
        AudioPlayerHandler._mode = validMode;

        const studioEl = document.getElementById('audio-studio');
        const backdropEl = document.getElementById('audio-studio-backdrop');
        if (studioEl) {
            studioEl.setAttribute('data-mode', validMode);
        }
        if (document.body) {
            document.body.dataset.audioMode = validMode;
        }
        if (backdropEl) {
            backdropEl.classList.toggle('hidden', validMode !== 'window');
        }

        if (validMode === 'bar') {
            AudioPlayerHandler._resetBarIdleTimer();
            AudioPlayerHandler._renderBarQueuePanel();
        } else {
            AudioPlayerHandler._clearBarIdleTimer();
        }

        AudioPlayerHandler.updateVisibilityForPage();

        if (validMode === 'window' && !options.skipLoad) {
            AudioPlayerHandler.refreshStudioData();
        }
    },

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

    toggleBarQueue(forceCollapsed = null) {
        const nextState = typeof forceCollapsed === 'boolean'
            ? forceCollapsed
            : !AudioPlayerHandler._barQueueCollapsed;
        AudioPlayerHandler._barQueueCollapsed = nextState;

        const panel = document.getElementById('audio-bar-queue-panel');
        if (panel) {
            panel.classList.toggle('collapsed', nextState);
        }
        const icon = document.querySelector('#audio-bar-queue-toggle-btn i');
        if (icon) {
            icon.className = nextState ? 'fas fa-chevron-up' : 'fas fa-chevron-down';
        }
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
            if (res && res.success && res.playlist) {
                AudioPlayerHandler._activePlaylistDetail = res.playlist;
            } else {
                AudioPlayerHandler._activePlaylistDetail = null;
            }
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

    /** Build a Fisher-Yates shuffle order across _queue, optionally placing anchorIndex first. */
    _rebuildShuffleDeck(anchorIndex = -1) {
        const len = AudioPlayerHandler._queue.length;
        const deck = Array.from({ length: len }, (_, i) => i);
        for (let i = deck.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            const tmp = deck[i];
            deck[i] = deck[j];
            deck[j] = tmp;
        }
        if (anchorIndex >= 0 && anchorIndex < len) {
            const pos = deck.indexOf(anchorIndex);
            if (pos > 0) {
                const tmp = deck[0];
                deck[0] = deck[pos];
                deck[pos] = tmp;
            }
            AudioPlayerHandler._shufflePos = 0;
        } else {
            AudioPlayerHandler._shufflePos = -1;
        }
        AudioPlayerHandler._shuffleOrder = deck;
    },

    /** Resolve the upcoming track to preload based on current loop/shuffle mode. */
    _getUpcomingPreloadTrack(index) {
        const queue = AudioPlayerHandler._queue;
        const len = queue.length;
        if (len <= 1 || AudioPlayerHandler._loopMode === 'loop-one') return null;

        let nextIdx = null;
        if (AudioPlayerHandler._loopMode === 'shuffle') {
            if (AudioPlayerHandler._shuffleOrder.length !== len) {
                AudioPlayerHandler._rebuildShuffleDeck(index);
            }
            const curPos = AudioPlayerHandler._shufflePos >= 0
                ? AudioPlayerHandler._shufflePos
                : AudioPlayerHandler._shuffleOrder.indexOf(index);
            const nextPos = curPos + 1;
            if (nextPos >= 0 && nextPos < AudioPlayerHandler._shuffleOrder.length) {
                nextIdx = AudioPlayerHandler._shuffleOrder[nextPos];
            }
        } else if (AudioPlayerHandler._loopMode === 'off') {
            nextIdx = index + 1;
            if (nextIdx >= len) return null;
        } else {
            nextIdx = (index + 1) % len;
        }

        if (nextIdx === null || nextIdx < 0 || nextIdx >= len || nextIdx === index) return null;
        const candidate = queue[nextIdx];
        return (candidate && !candidate.missing) ? candidate : null;
    },

    /** Replace active queue with tracks and start playing at startIndex (or shuffled). */
    async playTrackList(tracks, startIndex = 0, shuffle = false) {
        const playable = (tracks || []).filter(t => !t.missing);
        if (playable.length === 0) return;

        AudioPlayerHandler._queue = playable.slice();
        AudioPlayerHandler._playHistory = [];
        AudioPlayerHandler._shuffleOrder = [];
        AudioPlayerHandler._shufflePos = -1;

        let targetIndex = Math.max(0, Math.min(playable.length - 1, startIndex));
        if (shuffle) {
            AudioPlayerHandler._loopMode = 'shuffle';
            try {
                localStorage.setItem('tdrive_audio_loop_mode', 'shuffle');
            } catch (_err) {
                /* Ignore storage errors */
            }
            AudioPlayerHandler._rebuildShuffleDeck(-1);
            targetIndex = AudioPlayerHandler._shuffleOrder.length > 0 ? AudioPlayerHandler._shuffleOrder[0] : 0;
            AudioPlayerHandler._shufflePos = 0;
        } else if (AudioPlayerHandler._loopMode === 'shuffle') {
            AudioPlayerHandler._rebuildShuffleDeck(targetIndex);
        }

        AudioPlayerHandler.updateVisibilityForPage();
        if (AudioPlayerHandler._mode === 'mini') {
            AudioPlayerHandler.setMode('bar');
        }
        await AudioPlayerHandler._playTrackAtIndex(targetIndex, { pushHistory: false, shufflePos: AudioPlayerHandler._shufflePos });
    },

    async _playTrackAtIndex(index, options = {}) {
        if (index < 0 || index >= AudioPlayerHandler._queue.length) return;
        const reqSeq = ++AudioPlayerHandler._playRequestSeq;

        if (
            options.pushHistory !== false
            && AudioPlayerHandler._currentTrack
            && AudioPlayerHandler._currentIndex >= 0
            && AudioPlayerHandler._currentIndex !== index
        ) {
            AudioPlayerHandler._playHistory.push({
                index: AudioPlayerHandler._currentIndex,
                id: AudioPlayerHandler._currentTrack.id,
            });
            if (AudioPlayerHandler._playHistory.length > 100) {
                AudioPlayerHandler._playHistory.shift();
            }
        }

        if (AudioPlayerHandler._loopMode === 'shuffle') {
            if (typeof options.shufflePos === 'number' && options.shufflePos >= 0) {
                AudioPlayerHandler._shufflePos = options.shufflePos;
            } else {
                const pos = AudioPlayerHandler._shuffleOrder.indexOf(index);
                if (pos >= 0 && AudioPlayerHandler._shuffleOrder.length === AudioPlayerHandler._queue.length) {
                    AudioPlayerHandler._shufflePos = pos;
                } else {
                    AudioPlayerHandler._rebuildShuffleDeck(index);
                }
            }
        }

        const track = AudioPlayerHandler._queue[index];
        AudioPlayerHandler._currentIndex = index;
        AudioPlayerHandler._currentTrack = track;
        AudioPlayerHandler._playbackState = 'buffering';
        AudioPlayerHandler._currentMs = 0;
        AudioPlayerHandler._durationMs = 0;
        AudioPlayerHandler._seekGuardTargetMs = null;
        AudioPlayerHandler._seekGuardUntil = 0;
        AudioPlayerHandler._lastEndedTriggerKey = null;
        AudioPlayerHandler._lastErrorTriggerKey = null;

        AudioPlayerHandler._renderTransportUI();
        AudioPlayerHandler._renderStudioTrackList();

        // Warm the initial chunk of the upcoming track in background
        if (typeof ApiService.preloadAudio === 'function') {
            const nextTrack = AudioPlayerHandler._getUpcomingPreloadTrack(index);
            if (nextTrack && Number(nextTrack.id) !== Number(track.id)) {
                ApiService.preloadAudio(nextTrack.id);
            }
        }

        try {
            const res = await ApiService.playAudio(track.id);
            if (reqSeq !== AudioPlayerHandler._playRequestSeq || (res && res.stale)) {
                return;
            }
            if (!res || !res.success) {
                AudioPlayerHandler._playbackState = 'error';
                AudioPlayerHandler._renderTransportUI();
                UIManager.handleBackendError(res);
            } else if (res.state) {
                AudioPlayerHandler.handleBackendState(res.state);
            }
        } catch (err) {
            if (reqSeq !== AudioPlayerHandler._playRequestSeq) {
                return;
            }
            console.error('Failed to play audio track:', err);
            AudioPlayerHandler._playbackState = 'error';
            AudioPlayerHandler._renderTransportUI();
        }
    },

    async togglePlay() {
        if (!AudioPlayerHandler._currentTrack) {
            if (AudioPlayerHandler._allAudioFiles.length > 0) {
                await AudioPlayerHandler.playTrackList(AudioPlayerHandler._allAudioFiles, 0, false);
            }
            return;
        }
        if (AudioPlayerHandler._playbackState === 'error' && AudioPlayerHandler._currentIndex >= 0) {
            await AudioPlayerHandler._playTrackAtIndex(AudioPlayerHandler._currentIndex, { pushHistory: false });
            return;
        }
        try {
            const res = await ApiService.controlAudio('toggle', 0);
            if (res && res.state) AudioPlayerHandler.handleBackendState(res.state);
        } catch (err) {
            console.error('Failed to toggle audio playback:', err);
        }
    },

    async playNext(autoTriggered = false) {
        const len = AudioPlayerHandler._queue.length;
        if (len === 0) return;

        if (AudioPlayerHandler._loopMode === 'shuffle') {
            if (len === 1) {
                if (autoTriggered) return;
                await AudioPlayerHandler._playTrackAtIndex(0, { pushHistory: false, shufflePos: 0 });
                return;
            }
            if (AudioPlayerHandler._shuffleOrder.length !== len) {
                AudioPlayerHandler._rebuildShuffleDeck(AudioPlayerHandler._currentIndex);
            }
            let nextPos = AudioPlayerHandler._shufflePos + 1;
            if (nextPos >= AudioPlayerHandler._shuffleOrder.length) {
                AudioPlayerHandler._rebuildShuffleDeck(-1);
                if (AudioPlayerHandler._shuffleOrder[0] === AudioPlayerHandler._currentIndex && len > 1) {
                    const tmp = AudioPlayerHandler._shuffleOrder[0];
                    AudioPlayerHandler._shuffleOrder[0] = AudioPlayerHandler._shuffleOrder[1];
                    AudioPlayerHandler._shuffleOrder[1] = tmp;
                }
                nextPos = 0;
            }
            const nextIdx = AudioPlayerHandler._shuffleOrder[nextPos];
            await AudioPlayerHandler._playTrackAtIndex(nextIdx, { shufflePos: nextPos });
            return;
        }

        let nextIdx = AudioPlayerHandler._currentIndex + 1;
        if (nextIdx >= len) {
            if (autoTriggered && AudioPlayerHandler._loopMode === 'off') {
                return;
            }
            nextIdx = 0;
        }
        await AudioPlayerHandler._playTrackAtIndex(nextIdx);
    },

    async playPrev() {
        const len = AudioPlayerHandler._queue.length;
        if (len === 0) return;

        if (AudioPlayerHandler._currentMs > 3000) {
            await AudioPlayerHandler.seekTo(0);
            return;
        }

        if (AudioPlayerHandler._loopMode === 'shuffle' && AudioPlayerHandler._playHistory.length > 0) {
            while (AudioPlayerHandler._playHistory.length > 0) {
                const entry = AudioPlayerHandler._playHistory.pop();
                if (!entry) continue;
                let resolvedIdx = -1;
                if (entry.index >= 0 && entry.index < len && Number(AudioPlayerHandler._queue[entry.index].id) === Number(entry.id)) {
                    resolvedIdx = entry.index;
                } else {
                    resolvedIdx = AudioPlayerHandler._queue.findIndex(t => Number(t.id) === Number(entry.id));
                }
                if (resolvedIdx >= 0 && resolvedIdx !== AudioPlayerHandler._currentIndex) {
                    await AudioPlayerHandler._playTrackAtIndex(resolvedIdx, { pushHistory: false });
                    return;
                }
            }
        }

        let prevIdx = AudioPlayerHandler._currentIndex - 1;
        if (prevIdx < 0) prevIdx = len - 1;
        await AudioPlayerHandler._playTrackAtIndex(prevIdx, { pushHistory: false });
    },

    async seekTo(ms) {
        const target = Math.max(0, Math.round(ms));
        AudioPlayerHandler._currentMs = target;
        AudioPlayerHandler._seekGuardTargetMs = target;
        AudioPlayerHandler._seekGuardUntil = Date.now() + AudioPlayerHandler.SEEK_GUARD_MS;
        AudioPlayerHandler._renderProgressOnly();
        try {
            const res = await ApiService.controlAudio('seek', target);
            if (res && res.state) AudioPlayerHandler.handleBackendState(res.state);
        } catch (err) {
            console.error('Failed to seek audio:', err);
        }
    },

    async setVolume(vol, options = {}) {
        const clamped = Math.max(0, Math.min(100, Math.round(Number(vol) || 0)));
        AudioPlayerHandler._volume = clamped;
        AudioPlayerHandler._volumeGuardUntil = Date.now() + AudioPlayerHandler.VOLUME_GUARD_MS;
        AudioPlayerHandler._renderVolumeUI();

        const syncVolume = async () => {
            const latestVol = AudioPlayerHandler._volume;
            try {
                localStorage.setItem('tdrive_audio_volume', String(latestVol));
            } catch (_err) {
                /* Ignore storage errors */
            }
            try {
                await ApiService.controlAudio('volume', latestVol);
            } catch (err) {
                console.error('Failed to set audio volume:', err);
            }
        };

        if (AudioPlayerHandler._volumeSyncTimer) {
            clearTimeout(AudioPlayerHandler._volumeSyncTimer);
            AudioPlayerHandler._volumeSyncTimer = null;
        }
        if (options && options.immediate) {
            await syncVolume();
            return;
        }
        AudioPlayerHandler._volumeSyncTimer = setTimeout(() => {
            AudioPlayerHandler._volumeSyncTimer = null;
            syncVolume();
        }, AudioPlayerHandler.VOLUME_SYNC_DELAY_MS);
    },

    async toggleMute() {
        AudioPlayerHandler._muted = !AudioPlayerHandler._muted;
        AudioPlayerHandler._volumeGuardUntil = Date.now() + AudioPlayerHandler.VOLUME_GUARD_MS;
        try {
            localStorage.setItem('tdrive_audio_muted', AudioPlayerHandler._muted ? '1' : '0');
        } catch (_err) {
            /* Ignore storage errors */
        }
        AudioPlayerHandler._renderVolumeUI();
        try {
            await ApiService.controlAudio('mute', AudioPlayerHandler._muted ? 1 : 0);
        } catch (err) {
            console.error('Failed to toggle audio mute:', err);
        }
    },

    cycleLoopMode() {
        const order = ['off', 'loop-all', 'loop-one', 'shuffle'];
        const idx = order.indexOf(AudioPlayerHandler._loopMode);
        AudioPlayerHandler._loopMode = order[(idx + 1) % order.length];
        try {
            localStorage.setItem('tdrive_audio_loop_mode', AudioPlayerHandler._loopMode);
        } catch (_err) {
            /* Ignore storage errors */
        }
        if (AudioPlayerHandler._loopMode === 'shuffle' && AudioPlayerHandler._queue.length > 0) {
            AudioPlayerHandler._rebuildShuffleDeck(AudioPlayerHandler._currentIndex);
        }
        AudioPlayerHandler._renderTransportUI();
    },

    async stopPlayback() {
        AudioPlayerHandler._playRequestSeq += 1;
        try {
            await ApiService.controlAudio('stop', 0);
        } catch (err) {
            console.error('Failed to stop audio playback:', err);
        }
        AudioPlayerHandler._queue = [];
        AudioPlayerHandler._currentTrack = null;
        AudioPlayerHandler._currentIndex = -1;
        AudioPlayerHandler._playbackState = 'stopped';
        AudioPlayerHandler._currentMs = 0;
        AudioPlayerHandler._durationMs = 0;
        AudioPlayerHandler._seekGuardTargetMs = null;
        AudioPlayerHandler._seekGuardUntil = 0;
        AudioPlayerHandler._lastEndedTriggerKey = null;
        AudioPlayerHandler._lastErrorTriggerKey = null;
        AudioPlayerHandler._lastRenderedQueueKey = null;
        AudioPlayerHandler._shuffleOrder = [];
        AudioPlayerHandler._shufflePos = -1;
        AudioPlayerHandler._playHistory = [];
        AudioPlayerHandler.setMode('mini', { skipLoad: true });
        AudioPlayerHandler._renderTransportUI();
        AudioPlayerHandler._renderStudioTrackList();
    },

    reset() {
        AudioPlayerHandler.stopPlayback();
        AudioPlayerHandler._playRequestSeq += 1;
        if (AudioPlayerHandler._volumeSyncTimer) {
            clearTimeout(AudioPlayerHandler._volumeSyncTimer);
            AudioPlayerHandler._volumeSyncTimer = null;
        }
        AudioPlayerHandler._seekGuardTargetMs = null;
        AudioPlayerHandler._seekGuardUntil = 0;
        AudioPlayerHandler._isAdjustingVolume = false;
        AudioPlayerHandler._volumeGuardUntil = 0;
        AudioPlayerHandler._lastErrorTriggerKey = null;
        AudioPlayerHandler._shuffleOrder = [];
        AudioPlayerHandler._shufflePos = -1;
        AudioPlayerHandler._playHistory = [];
        AudioPlayerHandler._queue = [];
        AudioPlayerHandler._allAudioFiles = [];
        AudioPlayerHandler._playlists = [];
        AudioPlayerHandler._activePlaylistDetail = null;
        AudioPlayerHandler._draggingTrack = null;
        AudioPlayerHandler._selectedTrackIds.clear();
        AudioPlayerHandler._lastSelectedIndex = -1;
        AudioPlayerHandler._lastSelectedGroup = null;
        AudioPlayerHandler._lastRenderedQueueKey = null;
        if (AudioPlayerHandler._trackRenderState.observer) {
            AudioPlayerHandler._trackRenderState.observer.disconnect();
            AudioPlayerHandler._trackRenderState.observer = null;
        }
        AudioPlayerHandler._trackRenderState.tracks = [];
        AudioPlayerHandler._trackRenderState.index = 0;
        AudioPlayerHandler.toggleBarQueue(false);
        AudioPlayerHandler.updateVisibilityForPage();
        AudioPlayerHandler._renderBarQueuePanel(true);
    },

    /** Handle backend playback state snapshot from QWebChannel. */
    handleBackendState(state) {
        if (!state || typeof state !== 'object') return;
        const activeId = AudioPlayerHandler._currentTrack ? Number(AudioPlayerHandler._currentTrack.id) : null;
        if (state.file_id && activeId !== null && Number(state.file_id) !== activeId) {
            // Ignore stale events from a previously active track
            return;
        }

        const now = Date.now();
        AudioPlayerHandler._playbackState = state.state || 'stopped';
        if (!AudioPlayerHandler._isSeeking && typeof state.current_ms === 'number') {
            const seekGuardActive = AudioPlayerHandler._seekGuardTargetMs !== null
                && now < AudioPlayerHandler._seekGuardUntil;
            if (seekGuardActive && Math.abs(state.current_ms - AudioPlayerHandler._seekGuardTargetMs) > 2000) {
                // Keep optimistic seek target while libVLC fetches and jumps to the target byte range.
            } else {
                AudioPlayerHandler._seekGuardTargetMs = null;
                AudioPlayerHandler._currentMs = Math.max(0, state.current_ms);
            }
        }
        if (typeof state.duration_ms === 'number' && state.duration_ms >= 0) {
            AudioPlayerHandler._durationMs = state.duration_ms;
        }

        const volumeGuardActive = AudioPlayerHandler._isAdjustingVolume
            || AudioPlayerHandler._volumeSyncTimer !== null
            || now < AudioPlayerHandler._volumeGuardUntil;
        if (!volumeGuardActive) {
            if (typeof state.volume === 'number') {
                AudioPlayerHandler._volume = state.volume;
            }
            if (typeof state.muted === 'boolean') {
                AudioPlayerHandler._muted = state.muted;
            }
        }

        AudioPlayerHandler._renderTransportUI();

        if (AudioPlayerHandler._playbackState === 'error' && AudioPlayerHandler._currentTrack) {
            const errKey = `${AudioPlayerHandler._playRequestSeq}:${AudioPlayerHandler._currentIndex}:${AudioPlayerHandler._currentTrack.id}`;
            if (AudioPlayerHandler._lastErrorTriggerKey !== errKey) {
                AudioPlayerHandler._lastErrorTriggerKey = errKey;
                if (typeof UIModals !== 'undefined' && typeof UIModals.showAlert === 'function') {
                    const msg = AudioPlayerHandler._t(
                        'music.err_playback_failed',
                        'Unable to play "{name}". Please check your connection or verify the file still exists.'
                    ).replace('{name}', AudioPlayerHandler._currentTrack.name || '');
                    UIModals.showAlert(
                        AudioPlayerHandler._t('dialog.err_title', 'Error'),
                        msg,
                        'btn-primary'
                    );
                }
            }
            return;
        }

        if (AudioPlayerHandler._playbackState === 'ended' && AudioPlayerHandler._currentTrack) {
            const endKey = `${AudioPlayerHandler._currentIndex}:${AudioPlayerHandler._currentTrack.id}`;
            if (AudioPlayerHandler._lastEndedTriggerKey !== endKey) {
                AudioPlayerHandler._lastEndedTriggerKey = endKey;
                if (AudioPlayerHandler._loopMode === 'loop-one') {
                    AudioPlayerHandler._playTrackAtIndex(AudioPlayerHandler._currentIndex, { pushHistory: false });
                } else {
                    AudioPlayerHandler.playNext(true);
                }
            }
        }
    },

    _renderProgressOnly() {
        const curEl = document.getElementById('audio-time-current');
        const totEl = document.getElementById('audio-time-total');
        const seekSlider = document.getElementById('audio-seek-slider');
        if (curEl) curEl.textContent = AudioPlayerHandler.formatTime(AudioPlayerHandler._currentMs);
        if (totEl) totEl.textContent = AudioPlayerHandler.formatTime(AudioPlayerHandler._durationMs);
        if (seekSlider && !AudioPlayerHandler._isSeeking) {
            const ratio = AudioPlayerHandler._durationMs > 0
                ? Math.min(1000, Math.round((AudioPlayerHandler._currentMs / AudioPlayerHandler._durationMs) * 1000))
                : 0;
            seekSlider.value = String(ratio);
        }
    },

    _renderVolumeUI() {
        const muteBtn = document.getElementById('audio-mute-btn');
        const volSlider = document.getElementById('audio-volume-slider');
        if (volSlider) volSlider.value = String(AudioPlayerHandler._volume);
        if (muteBtn) {
            const icon = muteBtn.querySelector('i');
            if (icon) {
                if (AudioPlayerHandler._muted || AudioPlayerHandler._volume === 0) {
                    icon.className = 'fas fa-volume-mute';
                } else if (AudioPlayerHandler._volume < 50) {
                    icon.className = 'fas fa-volume-down';
                } else {
                    icon.className = 'fas fa-volume-up';
                }
            }
        }
    },

    _renderTransportUI() {
        const studioEl = document.getElementById('audio-studio');
        const isPlaying = AudioPlayerHandler._playbackState === 'playing';
        const isBuffering = AudioPlayerHandler._playbackState === 'buffering';
        const isError = AudioPlayerHandler._playbackState === 'error';
        if (studioEl) {
            studioEl.classList.toggle('is-playing', isPlaying);
            studioEl.classList.toggle('is-buffering', isBuffering);
            studioEl.classList.toggle('is-error', isError);
            studioEl.classList.toggle('has-track', Boolean(AudioPlayerHandler._currentTrack));
        }

        const track = AudioPlayerHandler._currentTrack;
        const miniTitle = document.getElementById('audio-mini-title');
        const barTitle = document.getElementById('audio-bar-title');
        const barSub = document.getElementById('audio-bar-subtitle');
        if (track) {
            if (miniTitle) {
                miniTitle.textContent = track.name;
                miniTitle.setAttribute('data-tooltip', track.name);
                miniTitle.setAttribute('data-tooltip-truncated', 'true');
                miniTitle.removeAttribute('title');
            }
            if (barTitle) {
                barTitle.textContent = track.name;
                barTitle.setAttribute('data-tooltip', track.name);
                barTitle.setAttribute('data-tooltip-truncated', 'true');
                barTitle.removeAttribute('title');
            }
            if (barSub) {
                const ext = track.name.includes('.') ? track.name.split('.').pop().toUpperCase() : 'AUDIO';
                const parts = [ext];
                if (track.size && track.size !== '-') parts.push(track.size);
                if (isError) {
                    parts.push(AudioPlayerHandler._t('music.status_error', 'Playback failed'));
                } else if (isBuffering) {
                    parts.push(AudioPlayerHandler._t('music.status_buffering', 'Buffering...'));
                }
                barSub.textContent = parts.join(' • ');
            }
        } else {
            if (miniTitle) {
                miniTitle.textContent = AudioPlayerHandler._t('music.library_title', 'Music Library');
                miniTitle.removeAttribute('data-tooltip');
                miniTitle.removeAttribute('title');
            }
            if (barTitle) {
                barTitle.textContent = AudioPlayerHandler._t('music.no_track_playing', 'No track selected');
                barTitle.removeAttribute('data-tooltip');
                barTitle.removeAttribute('title');
            }
            if (barSub) barSub.textContent = '';
        }

        const playIconClass = isBuffering
            ? 'fas fa-spinner fa-spin'
            : (isPlaying ? 'fas fa-pause' : 'fas fa-play');
        const miniPlayIcon = document.querySelector('#audio-mini-play-btn i');
        const barPlayIcon = document.querySelector('#audio-play-pause-btn i');
        if (miniPlayIcon) miniPlayIcon.className = isPlaying ? 'fas fa-pause' : 'fas fa-play';
        if (barPlayIcon) barPlayIcon.className = playIconClass;

        const loopBtn = document.getElementById('audio-loop-btn');
        if (loopBtn) {
            const loopIcon = loopBtn.querySelector('i');
            loopBtn.removeAttribute('data-i18n-title');
            loopBtn.removeAttribute('title');
            if (AudioPlayerHandler._loopMode === 'loop-all') {
                if (loopIcon) loopIcon.className = 'fas fa-redo-alt';
                loopBtn.dataset.loopState = 'all';
                loopBtn.setAttribute('data-i18n-tooltip', 'music.mode_loop_all');
                loopBtn.setAttribute('data-tooltip', AudioPlayerHandler._t('music.mode_loop_all', 'Repeat All'));
                loopBtn.classList.add('active');
            } else if (AudioPlayerHandler._loopMode === 'loop-one') {
                if (loopIcon) loopIcon.className = 'fas fa-redo-alt';
                loopBtn.dataset.loopState = 'one';
                loopBtn.setAttribute('data-i18n-tooltip', 'music.mode_loop_one');
                loopBtn.setAttribute('data-tooltip', AudioPlayerHandler._t('music.mode_loop_one', 'Repeat One'));
                loopBtn.classList.add('active');
            } else if (AudioPlayerHandler._loopMode === 'shuffle') {
                if (loopIcon) loopIcon.className = 'fas fa-random';
                loopBtn.dataset.loopState = 'shuffle';
                loopBtn.setAttribute('data-i18n-tooltip', 'music.mode_shuffle');
                loopBtn.setAttribute('data-tooltip', AudioPlayerHandler._t('music.mode_shuffle', 'Shuffle'));
                loopBtn.classList.add('active');
            } else {
                if (loopIcon) loopIcon.className = 'fas fa-redo';
                loopBtn.dataset.loopState = 'off';
                loopBtn.setAttribute('data-i18n-tooltip', 'music.mode_loop_off');
                loopBtn.setAttribute('data-tooltip', AudioPlayerHandler._t('music.mode_loop_off', 'No Repeat'));
                loopBtn.classList.remove('active');
            }
        }

        AudioPlayerHandler._renderProgressOnly();
        AudioPlayerHandler._renderVolumeUI();
        AudioPlayerHandler._renderBarQueuePanel();
    },

    _renderBarQueuePanel(force = false) {
        const listEl = document.getElementById('audio-bar-queue-list');
        const countEl = document.getElementById('audio-bar-queue-count');
        if (!listEl) return;

        const queue = AudioPlayerHandler._queue || [];
        if (countEl) countEl.textContent = String(queue.length);

        const queueKey = `${AudioPlayerHandler._currentIndex}|${queue.map(t => t.id).join(',')}`;
        if (!force && AudioPlayerHandler._lastRenderedQueueKey === queueKey) {
            return;
        }
        AudioPlayerHandler._lastRenderedQueueKey = queueKey;

        listEl.innerHTML = '';
        if (queue.length === 0) {
            const emptyDiv = document.createElement('div');
            emptyDiv.className = 'audio-bar-queue-empty';
            emptyDiv.textContent = AudioPlayerHandler._t('music.empty_queue', 'Playback queue is empty');
            listEl.appendChild(emptyDiv);
            return;
        }

        queue.forEach((track, idx) => {
            const isCurrent = idx === AudioPlayerHandler._currentIndex;
            const row = document.createElement('div');
            row.className = 'audio-bar-queue-row' + (isCurrent ? ' playing' : '');
            row.dataset.index = String(idx);
            row.dataset.id = String(track.id);

            const ext = AudioPlayerHandler._extractTrackExt(track.name);
            const idxHtml = isCurrent ? '<i class="fas fa-volume-up playing-indicator"></i>' : String(idx + 1);
            row.innerHTML = `
                <div class="audio-bar-queue-row-main">
                    <span class="audio-bar-queue-idx">${idxHtml}</span>
                    <span class="audio-bar-queue-name" data-tooltip="${UIManager.escapeHtml(track.name)}" data-tooltip-truncated="true">${UIManager.escapeHtml(track.name)}</span>
                </div>
                <span class="audio-bar-queue-meta">${UIManager.escapeHtml(ext)}</span>
                <button class="item-action-btn audio-queue-remove-btn" data-tooltip="${UIManager.escapeHtml(AudioPlayerHandler._t('music.remove_from_queue', 'Remove from Queue'))}"><i class="fas fa-times"></i></button>
            `;
            row.addEventListener('click', () => AudioPlayerHandler._playTrackAtIndex(idx));
            row.querySelector('.audio-queue-remove-btn')?.addEventListener('click', (e) => {
                e.stopPropagation();
                AudioPlayerHandler._removeQueueIndex(idx);
            });
            listEl.appendChild(row);
        });
    },

    async _removeQueueIndex(idx) {
        if (idx < 0 || idx >= AudioPlayerHandler._queue.length) return;
        const removed = AudioPlayerHandler._queue[idx];
        const wasCurrent = idx === AudioPlayerHandler._currentIndex;
        AudioPlayerHandler._queue.splice(idx, 1);
        if (removed) {
            AudioPlayerHandler._playHistory = AudioPlayerHandler._playHistory
                .filter(h => Number(h.id) !== Number(removed.id))
                .map(h => (h.index > idx ? { ...h, index: h.index - 1 } : h));
        }

        if (AudioPlayerHandler._queue.length === 0) {
            await AudioPlayerHandler.stopPlayback();
            return;
        }
        if (idx < AudioPlayerHandler._currentIndex) {
            AudioPlayerHandler._currentIndex -= 1;
            if (AudioPlayerHandler._loopMode === 'shuffle') {
                AudioPlayerHandler._rebuildShuffleDeck(AudioPlayerHandler._currentIndex);
            }
            AudioPlayerHandler._renderBarQueuePanel();
        } else if (wasCurrent) {
            const nextIndex = Math.min(idx, AudioPlayerHandler._queue.length - 1);
            if (AudioPlayerHandler._loopMode === 'shuffle') {
                AudioPlayerHandler._rebuildShuffleDeck(nextIndex);
            }
            await AudioPlayerHandler._playTrackAtIndex(nextIndex, { pushHistory: false });
        } else {
            if (AudioPlayerHandler._loopMode === 'shuffle') {
                AudioPlayerHandler._rebuildShuffleDeck(AudioPlayerHandler._currentIndex);
            }
            AudioPlayerHandler._renderBarQueuePanel();
        }
        AudioPlayerHandler.updateVisibilityForPage();
    },

    /** Synchronize _allAudioFiles and active _queue after cloud file deletions, moves, or renames. */
    async handleCloudFilesChanged() {
        try {
            const audioRes = await ApiService.getAllAudioFiles();
            if (!audioRes || !audioRes.success || !Array.isArray(audioRes.items)) {
                return;
            }
            AudioPlayerHandler._allAudioFiles = audioRes.items;
            const byId = new Map(audioRes.items.map(item => [Number(item.id), item]));

            if (AudioPlayerHandler._queue.length > 0) {
                const prevActiveId = AudioPlayerHandler._currentTrack ? Number(AudioPlayerHandler._currentTrack.id) : null;
                const prunedQueue = [];
                AudioPlayerHandler._queue.forEach((track) => {
                    const fresh = byId.get(Number(track.id));
                    if (fresh) {
                        prunedQueue.push({ ...track, ...fresh });
                    }
                });

                AudioPlayerHandler._queue = prunedQueue;
                if (prevActiveId !== null) {
                    const newIdx = prunedQueue.findIndex(t => Number(t.id) === prevActiveId);
                    if (newIdx >= 0) {
                        AudioPlayerHandler._currentIndex = newIdx;
                        AudioPlayerHandler._currentTrack = prunedQueue[newIdx];
                    } else if (AudioPlayerHandler._playbackState !== 'playing' && AudioPlayerHandler._playbackState !== 'buffering') {
                        await AudioPlayerHandler.stopPlayback();
                    } else {
                        AudioPlayerHandler._currentIndex = -1;
                    }
                }
                if (AudioPlayerHandler._loopMode === 'shuffle' && AudioPlayerHandler._queue.length > 0) {
                    AudioPlayerHandler._rebuildShuffleDeck(AudioPlayerHandler._currentIndex);
                }
                AudioPlayerHandler._lastRenderedQueueKey = null;
                AudioPlayerHandler._renderTransportUI();
                AudioPlayerHandler.updateVisibilityForPage();
            }

            if (AudioPlayerHandler._mode === 'window') {
                if (AudioPlayerHandler._studioView !== 'all' && AudioPlayerHandler._studioView !== 'folders') {
                    await AudioPlayerHandler._loadActivePlaylistDetail(AudioPlayerHandler._studioView);
                }
                AudioPlayerHandler._renderStudioSidebar();
                AudioPlayerHandler._renderStudioTrackList();
            }
        } catch (err) {
            console.error('Failed to sync audio state after cloud file change:', err);
        }
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
            if (!e.ctrlKey && !e.metaKey) {
                AudioPlayerHandler._selectedTrackIds.clear();
            }
            const start = Math.min(AudioPlayerHandler._lastSelectedIndex, indexInGroup);
            const end = Math.max(AudioPlayerHandler._lastSelectedIndex, indexInGroup);
            for (let i = start; i <= end; i++) {
                const candidate = groupList[i];
                if (candidate && !candidate.missing) {
                    AudioPlayerHandler._selectedTrackIds.add(Number(candidate.id));
                }
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

        const ext = AudioPlayerHandler._extractTrackExt(track.name);
        const displayIdx = displayIndexOverride !== null ? displayIndexOverride + 1 : indexInGroup + 1;
        const folderLabel = track.missing
            ? AudioPlayerHandler._t('music.missing_track', 'File moved or in trash')
            : (track.folder_path || 'TDrive');

        const playBtnHtml = track.missing
            ? ''
            : `<button class="item-action-btn audio-row-play-btn"><i class="fas fa-play"></i></button>`;
        const addBtnHtml = track.missing
            ? ''
            : `<button class="item-action-btn audio-row-add-btn" data-tooltip="${UIManager.escapeHtml(AudioPlayerHandler._t('music.add_to_playlist', 'Add to Playlist'))}"><i class="fas fa-plus-circle"></i></button>`;
        const removeBtnHtml = isCustomPlaylist
            ? `<button class="item-action-btn audio-remove-track-btn" data-tooltip="${UIManager.escapeHtml(AudioPlayerHandler._t('music.remove_from_playlist', 'Remove from playlist'))}"><i class="fas fa-times"></i></button>`
            : '';

        row.innerHTML = `
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
                    isCustomPlaylist
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

        return row;
    },

    async playCurrentStudioView(shuffle = false) {
        const tracks = AudioPlayerHandler._getActiveViewTracks();
        await AudioPlayerHandler.playTrackList(tracks, 0, shuffle);
    },

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

    _refreshDynamicLabels() {
        AudioPlayerHandler._lastRenderedQueueKey = null;
        AudioPlayerHandler._renderTransportUI();
        if (AudioPlayerHandler._mode === 'window') {
            AudioPlayerHandler._renderStudioSidebar();
            AudioPlayerHandler._renderStudioTrackList();
        }
    },
};
