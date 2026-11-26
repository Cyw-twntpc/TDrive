/**
 * Audio playback queue, Fisher-Yates shuffle deck, track navigation,
 * preload resolution, and cloud-change queue synchronization.
 */
Object.assign(AudioPlayerHandler, {
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

        if (typeof ApiService.preloadAudio === 'function') {
            const nextTrack = AudioPlayerHandler._getUpcomingPreloadTrack(index);
            if (nextTrack && Number(nextTrack.id) !== Number(track.id)) {
                ApiService.preloadAudio(nextTrack.id);
            }
        }

        try {
            const res = await ApiService.playAudio(track.id);
            if (reqSeq !== AudioPlayerHandler._playRequestSeq || (res && res.stale)) return;
            if (!res || !res.success) {
                AudioPlayerHandler._playbackState = 'error';
                AudioPlayerHandler._renderTransportUI();
                UIManager.handleBackendError(res);
            } else if (res.state) {
                AudioPlayerHandler.handleBackendState(res.state);
            }
        } catch (err) {
            if (reqSeq !== AudioPlayerHandler._playRequestSeq) return;
            console.error('Failed to play audio track:', err);
            AudioPlayerHandler._playbackState = 'error';
            AudioPlayerHandler._renderTransportUI();
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
            await AudioPlayerHandler._playTrackAtIndex(AudioPlayerHandler._shuffleOrder[nextPos], { shufflePos: nextPos });
            return;
        }

        let nextIdx = AudioPlayerHandler._currentIndex + 1;
        if (nextIdx >= len) {
            if (autoTriggered && AudioPlayerHandler._loopMode === 'off') return;
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

    _renderBarQueuePanel(force = false) {
        const listEl = document.getElementById('audio-bar-queue-list');
        const countEl = document.getElementById('audio-bar-queue-count');
        if (!listEl) return;

        const queue = AudioPlayerHandler._queue || [];
        if (countEl) countEl.textContent = String(queue.length);

        const queueKey = `${AudioPlayerHandler._currentIndex}|${queue.map(t => t.id).join(',')}`;
        if (!force && AudioPlayerHandler._lastRenderedQueueKey === queueKey) return;
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
            if (!audioRes || !audioRes.success || !Array.isArray(audioRes.items)) return;
            AudioPlayerHandler._allAudioFiles = audioRes.items;
            const byId = new Map(audioRes.items.map(item => [Number(item.id), item]));

            if (AudioPlayerHandler._queue.length > 0) {
                const prevActiveId = AudioPlayerHandler._currentTrack ? Number(AudioPlayerHandler._currentTrack.id) : null;
                const prunedQueue = [];
                AudioPlayerHandler._queue.forEach((track) => {
                    const fresh = byId.get(Number(track.id));
                    if (fresh) prunedQueue.push({ ...track, ...fresh });
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
});
