/**
 * Core audio playback controller, 3-stage Audio Studio mode transitions,
 * active queue/shuffle/history state, and QWebChannel synchronization.
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

    /** Initialize DOM bindings, persisted volume/loop state, and QWebChannel signals. */
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

    /** Bind click, slider, search, shortcut, and modal listeners idempotently. */
    _bindDomEvents() {
        if (AudioPlayerHandler._domBound) return;
        AudioPlayerHandler._domBound = true;
        AudioPlayerHandler._bindStageAndIdleEvents();
        AudioPlayerHandler._bindTransportEvents();
        AudioPlayerHandler._bindKeyboardShortcuts();
        AudioPlayerHandler._bindStudioControls();
        AudioPlayerHandler._bindPickerModalEvents();
    },

    _bindStageAndIdleEvents() {
        document.getElementById('audio-mini-expand-bar-btn')?.addEventListener('click', () => AudioPlayerHandler.setMode('bar'));
        document.getElementById('audio-mini-expand-window-btn')?.addEventListener('click', () => AudioPlayerHandler.setMode('window'));
        document.getElementById('audio-bar-open-window-btn')?.addEventListener('click', () => AudioPlayerHandler.setMode('window'));
        document.getElementById('audio-window-to-bar-btn')?.addEventListener('click', () => AudioPlayerHandler.setMode('bar'));
        document.getElementById('audio-window-to-mini-btn')?.addEventListener('click', () => AudioPlayerHandler.setMode('mini'));
        document.getElementById('audio-studio-backdrop')?.addEventListener('click', () => {
            AudioPlayerHandler.setMode(AudioPlayerHandler._currentTrack ? 'bar' : 'mini');
        });

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
    },

    _bindTransportEvents() {
        document.getElementById('audio-bar-queue-header')?.addEventListener('click', () => AudioPlayerHandler.toggleBarQueue());
        document.getElementById('audio-bar-queue-toggle-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            AudioPlayerHandler.toggleBarQueue();
        });

        document.getElementById('audio-mini-play-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            if (!AudioPlayerHandler._currentTrack) {
                AudioPlayerHandler.setMode('window');
                return;
            }
            AudioPlayerHandler.togglePlay();
        });

        document.getElementById('audio-play-pause-btn')?.addEventListener('click', () => AudioPlayerHandler.togglePlay());
        document.getElementById('audio-prev-btn')?.addEventListener('click', () => AudioPlayerHandler.playPrev());
        document.getElementById('audio-next-btn')?.addEventListener('click', () => AudioPlayerHandler.playNext(false));
        document.getElementById('audio-stop-btn')?.addEventListener('click', () => AudioPlayerHandler.stopPlayback());
        document.getElementById('audio-loop-btn')?.addEventListener('click', () => AudioPlayerHandler.cycleLoopMode());
        document.getElementById('audio-mute-btn')?.addEventListener('click', () => AudioPlayerHandler.toggleMute());

        const seekSlider = document.getElementById('audio-seek-slider');
        if (seekSlider) {
            const finishSeek = () => {
                if (!AudioPlayerHandler._isSeeking) return;
                AudioPlayerHandler._isSeeking = false;
                if (AudioPlayerHandler._mode === 'bar') AudioPlayerHandler._resetBarIdleTimer();
                setTimeout(() => {
                    if (!AudioPlayerHandler._isSeeking) AudioPlayerHandler._renderProgressOnly();
                }, 0);
            };
            seekSlider.addEventListener('mousedown', () => { AudioPlayerHandler._isSeeking = true; });
            seekSlider.addEventListener('touchstart', () => { AudioPlayerHandler._isSeeking = true; }, { passive: true });
            window.addEventListener('mouseup', finishSeek);
            window.addEventListener('touchend', finishSeek);
            window.addEventListener('touchcancel', finishSeek);
            seekSlider.addEventListener('blur', finishSeek);
            seekSlider.addEventListener('input', () => {
                seekSlider.style.setProperty('--fill-pct', `${(Number(seekSlider.value) / 10).toFixed(1)}%`);
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

        const volSlider = document.getElementById('audio-volume-slider');
        if (volSlider) {
            const endVolumeAdjust = () => { AudioPlayerHandler._isAdjustingVolume = false; };
            volSlider.addEventListener('mousedown', () => { AudioPlayerHandler._isAdjustingVolume = true; });
            volSlider.addEventListener('touchstart', () => { AudioPlayerHandler._isAdjustingVolume = true; }, { passive: true });
            window.addEventListener('mouseup', endVolumeAdjust);
            window.addEventListener('touchend', endVolumeAdjust);
            window.addEventListener('touchcancel', endVolumeAdjust);
            volSlider.addEventListener('blur', endVolumeAdjust);
            volSlider.addEventListener('input', () => AudioPlayerHandler.setVolume(Number(volSlider.value)));
            volSlider.addEventListener('change', () => {
                AudioPlayerHandler._isAdjustingVolume = false;
                AudioPlayerHandler.setVolume(Number(volSlider.value), { immediate: true });
            });
        }
    },

    _bindKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            if (e.ctrlKey || e.metaKey || e.altKey) return;
            const target = e.target;
            const tagName = (target && target.tagName) ? target.tagName.toUpperCase() : '';
            if (tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT' || (target && target.isContentEditable)) {
                return;
            }
            if (document.querySelector('.modal-overlay:not(.hidden)')) return;

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

            if ((mode !== 'bar' && mode !== 'window') || !AudioPlayerHandler._currentTrack) return;
            if (e.code === 'Space' || e.key === ' ') {
                e.preventDefault();
                AudioPlayerHandler.togglePlay();
            } else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && AudioPlayerHandler._durationMs > 0) {
                e.preventDefault();
                const delta = e.key === 'ArrowRight' ? 5000 : -5000;
                const targetMs = Math.max(0, Math.min(AudioPlayerHandler._durationMs, AudioPlayerHandler._currentMs + delta));
                AudioPlayerHandler.seekTo(targetMs);
            }
        });
    },

    _bindStudioControls() {
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
    },

    _bindPickerModalEvents() {
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
        if (studioEl) studioEl.setAttribute('data-mode', validMode);
        if (document.body) document.body.dataset.audioMode = validMode;
        if (backdropEl) backdropEl.classList.toggle('hidden', validMode !== 'window');

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

    toggleBarQueue(forceCollapsed = null) {
        const nextState = typeof forceCollapsed === 'boolean'
            ? forceCollapsed
            : !AudioPlayerHandler._barQueueCollapsed;
        AudioPlayerHandler._barQueueCollapsed = nextState;

        const panel = document.getElementById('audio-bar-queue-panel');
        if (panel) panel.classList.toggle('collapsed', nextState);
        const icon = document.querySelector('#audio-bar-queue-toggle-btn i');
        if (icon) icon.className = nextState ? 'fas fa-chevron-up' : 'fas fa-chevron-down';
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
            return;
        }

        const now = Date.now();
        AudioPlayerHandler._playbackState = state.state || 'stopped';
        if (!AudioPlayerHandler._isSeeking && typeof state.current_ms === 'number') {
            const seekGuardActive = AudioPlayerHandler._seekGuardTargetMs !== null
                && now < AudioPlayerHandler._seekGuardUntil;
            if (!(seekGuardActive && Math.abs(state.current_ms - AudioPlayerHandler._seekGuardTargetMs) > 2000)) {
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
            if (typeof state.volume === 'number') AudioPlayerHandler._volume = state.volume;
            if (typeof state.muted === 'boolean') AudioPlayerHandler._muted = state.muted;
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
                    UIModals.showAlert(AudioPlayerHandler._t('dialog.err_title', 'Error'), msg, 'btn-primary');
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
            seekSlider.style.setProperty('--fill-pct', `${(ratio / 10).toFixed(1)}%`);
        }
    },

    _renderVolumeUI() {
        const muteBtn = document.getElementById('audio-mute-btn');
        const volSlider = document.getElementById('audio-volume-slider');
        if (volSlider) {
            volSlider.value = String(AudioPlayerHandler._volume);
            volSlider.style.setProperty('--fill-pct', `${AudioPlayerHandler._volume}%`);
        }
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

    _renderLoopButton() {
        const loopBtn = document.getElementById('audio-loop-btn');
        if (!loopBtn) return;

        const loopMeta = {
            'loop-all': { icon: 'fas fa-redo-alt', state: 'all', key: 'music.mode_loop_all', fallback: 'Repeat All', active: true },
            'loop-one': { icon: 'fas fa-redo-alt', state: 'one', key: 'music.mode_loop_one', fallback: 'Repeat One', active: true },
            shuffle: { icon: 'fas fa-random', state: 'shuffle', key: 'music.mode_shuffle', fallback: 'Shuffle', active: true },
            off: { icon: 'fas fa-redo', state: 'off', key: 'music.mode_loop_off', fallback: 'No Repeat', active: false },
        }[AudioPlayerHandler._loopMode] || {
            icon: 'fas fa-redo',
            state: 'off',
            key: 'music.mode_loop_off',
            fallback: 'No Repeat',
            active: false,
        };

        const loopIcon = loopBtn.querySelector('i');
        if (loopIcon) loopIcon.className = loopMeta.icon;
        loopBtn.dataset.loopState = loopMeta.state;
        loopBtn.removeAttribute('data-i18n-title');
        loopBtn.removeAttribute('title');
        loopBtn.setAttribute('data-i18n-tooltip', loopMeta.key);
        loopBtn.setAttribute('data-tooltip', AudioPlayerHandler._t(loopMeta.key, loopMeta.fallback));
        loopBtn.classList.toggle('active', loopMeta.active);
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
            [miniTitle, barTitle].forEach((el) => {
                if (!el) return;
                el.textContent = track.name;
                el.setAttribute('data-tooltip', track.name);
                el.setAttribute('data-tooltip-truncated', 'true');
                el.removeAttribute('title');
            });
            if (barSub) {
                const ext = track.name.includes('.') ? track.name.split('.').pop().toUpperCase() : 'AUDIO';
                const parts = [ext];
                if (track.size && track.size !== '-') parts.push(track.size);
                if (isError) parts.push(AudioPlayerHandler._t('music.status_error', 'Playback failed'));
                else if (isBuffering) parts.push(AudioPlayerHandler._t('music.status_buffering', 'Buffering...'));
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

        const playIconClass = isBuffering ? 'fas fa-spinner fa-spin' : (isPlaying ? 'fas fa-pause' : 'fas fa-play');
        const miniPlayIcon = document.querySelector('#audio-mini-play-btn i');
        const barPlayIcon = document.querySelector('#audio-play-pause-btn i');
        if (miniPlayIcon) miniPlayIcon.className = isPlaying ? 'fas fa-pause' : 'fas fa-play';
        if (barPlayIcon) barPlayIcon.className = playIconClass;

        AudioPlayerHandler._renderLoopButton();
        AudioPlayerHandler._renderProgressOnly();
        AudioPlayerHandler._renderVolumeUI();
        AudioPlayerHandler._renderBarQueuePanel();
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
