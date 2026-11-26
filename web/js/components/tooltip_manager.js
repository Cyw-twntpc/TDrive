/**
 * Centralized floating tooltip manager replacing native browser title tooltips.
 * Reads `data-tooltip` (plus `data-tooltip-truncated` / `data-tooltip-pos`) and
 * renders a viewport-clamped floating bubble attached directly to document.body.
 */
const TooltipManager = {
    SHOW_DELAY_MS: 260,
    WARM_WINDOW_MS: 160,
    GAP_PX: 9,
    VIEWPORT_PAD_PX: 8,

    _initialized: false,
    _el: null,
    _activeTarget: null,
    _pendingTarget: null,
    _showTimer: null,
    _lastHiddenAt: 0,
    _observer: null,

    /** Create or reuse the singleton tooltip node and attach delegated listeners. */
    init() {
        if (TooltipManager._initialized) return;
        TooltipManager._initialized = true;

        let el = document.getElementById('tdrive-tooltip');
        if (!el) {
            el = document.createElement('div');
            el.id = 'tdrive-tooltip';
            el.className = 'tdrive-tooltip';
            el.setAttribute('role', 'tooltip');
            el.setAttribute('aria-hidden', 'true');
            document.body.appendChild(el);
        }
        TooltipManager._el = el;

        document.addEventListener('mouseover', (e) => {
            const target = e.target && typeof e.target.closest === 'function'
                ? e.target.closest('[data-tooltip]')
                : null;
            if (!target) return;
            if (e.relatedTarget && target.contains(e.relatedTarget)) return;
            TooltipManager.show(target, false);
        });

        document.addEventListener('mouseout', (e) => {
            const current = TooltipManager._activeTarget || TooltipManager._pendingTarget;
            if (!current) return;
            const leftEl = e.target && typeof e.target.closest === 'function'
                ? e.target.closest('[data-tooltip]')
                : null;
            if (leftEl !== current) return;
            if (e.relatedTarget && current.contains(e.relatedTarget)) return;
            TooltipManager.hide();
        });

        document.addEventListener('focusin', (e) => {
            const target = e.target && typeof e.target.closest === 'function'
                ? e.target.closest('[data-tooltip]')
                : null;
            if (!target) return;
            // Only show on keyboard focus when supported, skipping mouse click focus noise
            if (typeof target.matches === 'function') {
                try {
                    if (!target.matches(':focus-visible')) return;
                } catch (_err) {
                    /* Fallback for environments without :focus-visible */
                }
            }
            TooltipManager.show(target, true);
        });

        document.addEventListener('focusout', () => TooltipManager.hide());
        document.addEventListener('pointerdown', () => TooltipManager.hide(), true);
        document.addEventListener('scroll', () => TooltipManager.hide(), true);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') TooltipManager.hide();
        }, true);
        window.addEventListener('blur', () => TooltipManager.hide());
        window.addEventListener('resize', () => {
            if (TooltipManager._activeTarget) {
                TooltipManager.position(TooltipManager._activeTarget);
            }
        });
    },

    /** Return true when an element's text overflows its visible box. */
    isTruncated(el) {
        if (!el) return false;
        return el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
    },

    /** Programmatically update tooltip attributes and refresh an active bubble. */
    set(el, text, options = {}) {
        if (!el) return;
        el.removeAttribute('title');
        const cleaned = text === undefined || text === null ? '' : String(text).trim();
        if (cleaned) {
            el.setAttribute('data-tooltip', cleaned);
        } else {
            el.removeAttribute('data-tooltip');
        }
        if (options.i18nKey) {
            el.setAttribute('data-i18n-tooltip', options.i18nKey);
        } else if (options.i18nKey === null) {
            el.removeAttribute('data-i18n-tooltip');
        }
        if (options.truncated === true) {
            el.setAttribute('data-tooltip-truncated', 'true');
        } else if (options.truncated === false) {
            el.removeAttribute('data-tooltip-truncated');
        }
        if (options.pos) {
            el.setAttribute('data-tooltip-pos', options.pos);
        } else if (options.pos === null) {
            el.removeAttribute('data-tooltip-pos');
        }
        if (TooltipManager._activeTarget === el) {
            TooltipManager.refresh(el);
        }
    },

    /** Validate visibility rules and reveal the tooltip immediately or after hover delay. */
    show(targetEl, immediate = false) {
        if (!TooltipManager._initialized) TooltipManager.init();
        if (!targetEl || !targetEl.isConnected) {
            TooltipManager.hide();
            return;
        }
        if (targetEl.hasAttribute('title')) {
            targetEl.removeAttribute('title');
        }
        const text = (targetEl.getAttribute('data-tooltip') || '').trim();
        if (!text) {
            TooltipManager.hide();
            return;
        }
        if (targetEl.getAttribute('data-tooltip-truncated') === 'true' && !TooltipManager.isTruncated(targetEl)) {
            TooltipManager.hide();
            return;
        }

        TooltipManager._clearTimer();
        const isWarm = (Date.now() - TooltipManager._lastHiddenAt) <= TooltipManager.WARM_WINDOW_MS;
        if (immediate || isWarm) {
            TooltipManager._reveal(targetEl, text);
            return;
        }

        TooltipManager._pendingTarget = targetEl;
        TooltipManager._showTimer = setTimeout(() => {
            TooltipManager._showTimer = null;
            if (TooltipManager._pendingTarget === targetEl && targetEl.isConnected) {
                const latestText = (targetEl.getAttribute('data-tooltip') || '').trim();
                if (latestText) {
                    TooltipManager._reveal(targetEl, latestText);
                }
            }
        }, TooltipManager.SHOW_DELAY_MS);
    },

    /** Render text, bind attribute observer for live state updates, and position bubble. */
    _reveal(targetEl, text) {
        const el = TooltipManager._el;
        if (!el) return;

        TooltipManager._disconnectObserver();
        TooltipManager._pendingTarget = null;
        TooltipManager._activeTarget = targetEl;
        el.textContent = text;

        if (typeof MutationObserver === 'function') {
            TooltipManager._observer = new MutationObserver(() => {
                TooltipManager.refresh(targetEl);
            });
            TooltipManager._observer.observe(targetEl, {
                attributes: true,
                attributeFilter: ['data-tooltip', 'data-tooltip-pos', 'data-tooltip-truncated'],
            });
        }

        TooltipManager.position(targetEl);
        el.setAttribute('aria-hidden', 'false');
        el.classList.add('is-visible');
    },

    /** Compute viewport-aware coordinates with auto-flipping and centered arrow offsets. */
    position(targetEl) {
        const el = TooltipManager._el;
        if (!el || !targetEl || !targetEl.isConnected) return;

        const rect = targetEl.getBoundingClientRect();
        const vw = window.innerWidth || document.documentElement.clientWidth || 1024;
        const vh = window.innerHeight || document.documentElement.clientHeight || 768;
        const gap = TooltipManager.GAP_PX;
        const pad = TooltipManager.VIEWPORT_PAD_PX;

        let placement = targetEl.getAttribute('data-tooltip-pos') || 'top';
        el.setAttribute('data-placement', placement);
        // Measure natural rendered size at fixed origin before placing
        el.style.left = '0px';
        el.style.top = '0px';
        const tipRect = el.getBoundingClientRect();
        const tipW = tipRect.width || el.offsetWidth || 0;
        const tipH = tipRect.height || el.offsetHeight || 0;

        const spaceTop = rect.top - pad;
        const spaceBottom = vh - rect.bottom - pad;
        const spaceLeft = rect.left - pad;
        const spaceRight = vw - rect.right - pad;

        if (placement === 'top' && spaceTop < tipH + gap && spaceBottom >= spaceTop) {
            placement = 'bottom';
        } else if (placement === 'bottom' && spaceBottom < tipH + gap && spaceTop >= spaceBottom) {
            placement = 'top';
        } else if (placement === 'left' && spaceLeft < tipW + gap && spaceRight >= spaceLeft) {
            placement = 'right';
        } else if (placement === 'right' && spaceRight < tipW + gap && spaceLeft >= spaceRight) {
            placement = 'left';
        }

        let left = 0;
        let top = 0;
        const centerX = rect.left + (rect.width / 2);
        const centerY = rect.top + (rect.height / 2);

        if (placement === 'bottom') {
            left = centerX - (tipW / 2);
            top = rect.bottom + gap;
        } else if (placement === 'left') {
            left = rect.left - tipW - gap;
            top = centerY - (tipH / 2);
        } else if (placement === 'right') {
            left = rect.right + gap;
            top = centerY - (tipH / 2);
        } else {
            placement = 'top';
            left = centerX - (tipW / 2);
            top = rect.top - tipH - gap;
        }

        left = Math.max(pad, Math.min(vw - tipW - pad, left));
        top = Math.max(pad, Math.min(vh - tipH - pad, top));

        const arrowX = Math.max(12, Math.min(Math.max(12, tipW - 12), centerX - left));
        const arrowY = Math.max(12, Math.min(Math.max(12, tipH - 12), centerY - top));

        el.setAttribute('data-placement', placement);
        el.style.left = `${Math.round(left)}px`;
        el.style.top = `${Math.round(top)}px`;
        el.style.setProperty('--tooltip-arrow-x', `${Math.round(arrowX)}px`);
        el.style.setProperty('--tooltip-arrow-y', `${Math.round(arrowY)}px`);
    },

    /** Synchronize active tooltip text and position when target attributes mutate. */
    refresh(targetEl) {
        if (!targetEl || targetEl !== TooltipManager._activeTarget) return;
        if (!targetEl.isConnected) {
            TooltipManager.hide();
            return;
        }
        const text = (targetEl.getAttribute('data-tooltip') || '').trim();
        if (!text || (targetEl.getAttribute('data-tooltip-truncated') === 'true' && !TooltipManager.isTruncated(targetEl))) {
            TooltipManager.hide();
            return;
        }
        if (TooltipManager._el) {
            TooltipManager._el.textContent = text;
            TooltipManager.position(targetEl);
        }
    },

    /** Clear pending reveal timers, disconnect observer, and hide the bubble. */
    hide() {
        TooltipManager._clearTimer();
        TooltipManager._disconnectObserver();
        const hadActive = Boolean(TooltipManager._activeTarget);
        TooltipManager._pendingTarget = null;
        TooltipManager._activeTarget = null;
        if (TooltipManager._el) {
            TooltipManager._el.classList.remove('is-visible');
            TooltipManager._el.setAttribute('aria-hidden', 'true');
        }
        if (hadActive) {
            TooltipManager._lastHiddenAt = Date.now();
        }
    },

    _clearTimer() {
        if (TooltipManager._showTimer) {
            clearTimeout(TooltipManager._showTimer);
            TooltipManager._showTimer = null;
        }
    },

    _disconnectObserver() {
        if (TooltipManager._observer) {
            TooltipManager._observer.disconnect();
            TooltipManager._observer = null;
        }
    },
};

window.TooltipManager = TooltipManager;

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => TooltipManager.init());
} else {
    TooltipManager.init();
}
