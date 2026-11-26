/**
 * Shared marquee drag-selection and edge auto-scroll engine for file and trash lists.
 */
const SelectionManager = {
    /**
     * Bind marquee box selection with batched geometry reads and smooth edge auto-scroll.
     *
     * @param {Object} config
     * @param {HTMLElement} config.containerEl - Scrollable section hosting the selection box.
     * @param {Function} config.getSelectionBox - Returns the marquee selection box DOM element.
     * @param {string} config.headerSelector - Selector for table header offset calculation.
     * @param {string} config.itemSelector - Selector for all rendered row/grid elements.
     * @param {string} config.selectableSelector - Selector for selectable item elements during drag.
     * @param {string} config.ignoreSelector - Selector for child controls that must not start drag-select.
     * @param {Function} [config.getScopeRoot] - Returns the DOM root holding item elements (defaults to document).
     * @param {Function} config.buildItemLookup - Returns a Map of "type:id" -> raw item data.
     * @param {Function} config.toSelectedEntry - Maps (itemData, itemType) -> object stored in AppState.selectedItems.
     * @param {Function} [config.onUpdate] - Callback invoked whenever selection changes.
     */
    bind(config) {
        const containerEl = config.containerEl;
        if (!containerEl) return;

        let isDragging = false;
        let startX = 0;
        let startY = 0;
        let autoScrollFrameId = null;
        let lastClientX = 0;
        let lastClientY = 0;

        const getHeaderHeight = () => {
            const headerEl = config.headerSelector ? containerEl.querySelector(config.headerSelector) : null;
            return headerEl ? headerEl.offsetHeight : 0;
        };

        const querySelectable = (selector) => {
            const scopeRoot = typeof config.getScopeRoot === 'function' ? (config.getScopeRoot() || document) : document;
            return scopeRoot.querySelectorAll(selector);
        };

        const updateSelectionBox = (clientX, clientY) => {
            const boxEl = config.getSelectionBox();
            if (!boxEl) return;

            const rect = containerEl.getBoundingClientRect();
            const viewTop = rect.top + getHeaderHeight();
            const clampedX = Math.max(rect.left, Math.min(rect.right, clientX));
            const clampedY = Math.max(viewTop, Math.min(rect.bottom, clientY));

            const currentContentX = clampedX - rect.left;
            const currentContentY = clampedY - rect.top + containerEl.scrollTop;

            Object.assign(boxEl.style, {
                left: `${Math.min(startX, currentContentX)}px`,
                top: `${Math.min(startY, currentContentY)}px`,
                width: `${Math.abs(startX - currentContentX)}px`,
                height: `${Math.abs(startY - currentContentY)}px`,
            });

            const boxRect = boxEl.getBoundingClientRect();
            const itemLookup = config.buildItemLookup();
            const selectedKeySet = new Set(AppState.selectedItems.map(i => `${i.type}:${i.id}`));
            const ctrlHeld = Boolean(window.event?.ctrlKey);

            // Batch geometry reads before mutating classes to prevent layout thrashing
            const measurements = [];
            querySelectable(config.selectableSelector).forEach(itemEl => {
                const itemRect = itemEl.getBoundingClientRect();
                const intersects = !(
                    boxRect.right < itemRect.left
                    || boxRect.left > itemRect.right
                    || boxRect.bottom < itemRect.top
                    || boxRect.top > itemRect.bottom
                );
                measurements.push({
                    itemEl,
                    intersects,
                    itemId: parseFloat(itemEl.dataset.id),
                    itemType: itemEl.dataset.type,
                });
            });

            let removedAny = false;
            measurements.forEach(({ itemEl, intersects, itemId, itemType }) => {
                const key = `${itemType}:${itemId}`;
                const isSelected = selectedKeySet.has(key);

                if (intersects) {
                    if (!isSelected) {
                        itemEl.classList.add('selected');
                        const itemData = itemLookup.get(key);
                        if (itemData) {
                            const entry = typeof config.toSelectedEntry === 'function'
                                ? config.toSelectedEntry(itemData, itemType)
                                : itemData;
                            AppState.selectedItems.push(entry);
                            selectedKeySet.add(key);
                        }
                    }
                } else if (!ctrlHeld && isSelected) {
                    itemEl.classList.remove('selected');
                    selectedKeySet.delete(key);
                    removedAny = true;
                }
            });

            if (removedAny) {
                const retained = AppState.selectedItems.filter(i => selectedKeySet.has(`${i.type}:${i.id}`));
                AppState.selectedItems.splice(0, AppState.selectedItems.length, ...retained);
            }
            if (typeof config.onUpdate === 'function') config.onUpdate(AppState);
        };

        const autoScrollLoop = () => {
            if (!isDragging) return;
            const rect = containerEl.getBoundingClientRect();
            const viewTop = rect.top + getHeaderHeight();
            let scrolled = false;

            const BASE_SPEED = 2;
            const MAX_SPEED = 30;
            const SENSITIVITY = 0.4;

            if (lastClientY < viewTop && containerEl.scrollTop > 0) {
                const speed = Math.min(MAX_SPEED, BASE_SPEED + ((viewTop - lastClientY) * SENSITIVITY));
                containerEl.scrollTop -= speed;
                scrolled = true;
            } else if (lastClientY > rect.bottom) {
                const maxScroll = containerEl.scrollHeight - containerEl.clientHeight;
                if (containerEl.scrollTop < maxScroll) {
                    const speed = Math.min(MAX_SPEED, BASE_SPEED + ((lastClientY - rect.bottom) * SENSITIVITY));
                    containerEl.scrollTop += speed;
                    scrolled = true;
                }
            }

            if (scrolled) updateSelectionBox(lastClientX, lastClientY);
            autoScrollFrameId = requestAnimationFrame(autoScrollLoop);
        };

        containerEl.addEventListener('mousedown', (e) => {
            const clickedItem = e.target.closest(config.itemSelector);
            if (clickedItem && clickedItem.classList.contains('selected')) return;
            if (config.ignoreSelector && e.target.closest(config.ignoreSelector)) return;

            const rect = containerEl.getBoundingClientRect();
            if (e.clientY < rect.top + getHeaderHeight()) return;

            const boxEl = config.getSelectionBox();
            if (!boxEl) return;

            containerEl.classList.add('is-selecting');
            isDragging = false;
            startX = e.clientX - rect.left;
            startY = e.clientY - rect.top + containerEl.scrollTop;

            Object.assign(boxEl.style, {
                left: `${startX}px`,
                top: `${startY}px`,
                width: '0px',
                height: '0px',
                display: 'block',
            });

            if (!clickedItem && !e.ctrlKey) {
                querySelectable(`${config.itemSelector}.selected`).forEach(el => el.classList.remove('selected'));
                AppState.selectedItems.length = 0;
                if (typeof config.onUpdate === 'function') config.onUpdate(AppState);
            }

            const onMouseMove = (moveE) => {
                lastClientX = moveE.clientX;
                lastClientY = moveE.clientY;

                if (!isDragging) {
                    if (Math.abs(moveE.clientX - e.clientX) < 5 && Math.abs(moveE.clientY - e.clientY) < 5) return;
                    isDragging = true;
                    moveE.preventDefault();
                    if (!e.ctrlKey) {
                        querySelectable(`${config.itemSelector}.selected`).forEach(el => el.classList.remove('selected'));
                        AppState.selectedItems.length = 0;
                    }
                    if (!autoScrollFrameId) autoScrollLoop();
                }
                updateSelectionBox(moveE.clientX, moveE.clientY);
            };

            const onMouseUp = () => {
                containerEl.classList.remove('is-selecting');
                isDragging = false;
                boxEl.style.display = 'none';
                if (autoScrollFrameId) {
                    cancelAnimationFrame(autoScrollFrameId);
                    autoScrollFrameId = null;
                }
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                if (typeof config.onUpdate === 'function') config.onUpdate(AppState);
            };

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    },
};

window.SelectionManager = SelectionManager;
