/**
 * @fileoverview Manages the full-screen image gallery with filmstrip and preloading.
 */
const GalleryHandler = {
    modal: null,
    imageEl: null,
    filenameEl: null,
    closeBtn: null,
    prevBtn: null,
    nextBtn: null,
    spinner: null,
    filmstripEl: null,

    currentImages: [], // Array of file objects {id, name, ...}
    currentIndex: -1,
    _showReqSeq: 0,
    
    // Drag to scroll variables
    isDown: false,
    startX: 0,
    scrollLeft: 0,

    init() {
        this.modal = document.getElementById('gallery-modal');
        if (!this.modal) return; // HTML not injected yet

        this.imageEl = document.getElementById('gallery-image');
        this.filenameEl = document.getElementById('gallery-filename');
        this.closeBtn = document.getElementById('gallery-close-btn');
        this.prevBtn = document.getElementById('gallery-prev-btn');
        this.nextBtn = document.getElementById('gallery-next-btn');
        this.spinner = document.getElementById('gallery-spinner');
        this.filmstripEl = document.getElementById('gallery-filmstrip');

        this.closeBtn.addEventListener('click', () => this.close());
        this.prevBtn.addEventListener('click', (e) => { e.stopPropagation(); this.nav(-1); });
        this.nextBtn.addEventListener('click', (e) => { e.stopPropagation(); this.nav(1); });
        
        // Toggle Fullscreen
        this.imageEl.addEventListener('click', (e) => {
            e.stopPropagation();
            this.modal.classList.toggle('fullscreen-mode');
        });

        // Drag to scroll logic for filmstrip
        this.filmstripEl.addEventListener('mousedown', (e) => {
            this.isDown = true;
            this.filmstripEl.classList.add('active'); // Optional: for cursor grabbing style if needed
            this.startX = e.pageX - this.filmstripEl.offsetLeft;
            this.scrollLeft = this.filmstripEl.scrollLeft;
        });
        
        this.filmstripEl.addEventListener('mouseleave', () => {
            this.isDown = false;
        });
        
        this.filmstripEl.addEventListener('mouseup', () => {
            this.isDown = false;
        });
        
        this.filmstripEl.addEventListener('mousemove', (e) => {
            if (!this.isDown) return;
            e.preventDefault();
            const x = e.pageX - this.filmstripEl.offsetLeft;
            const walk = (x - this.startX) * 2; // Scroll-fast
            this.filmstripEl.scrollLeft = this.scrollLeft - walk;
        });

        // Keyboard nav
        document.addEventListener('keydown', (e) => {
            if (this.modal.classList.contains('hidden')) return;
            if (e.key === 'Escape') this.close();
            if (e.key === 'ArrowLeft') this.nav(-1);
            if (e.key === 'ArrowRight') this.nav(1);
        });
    },

    openGallery(startFileId) {
        if (!this.modal) this.init(); // Lazy init if needed
        
        // Filter images from current folder
        let images = AppState.currentFolderContents.files.filter(f => {
            const ext = f.name.split('.').pop().toLowerCase();
            return ['jpg', 'jpeg', 'png', 'gif', 'webp'].includes(ext);
        });

        if (images.length === 0) return;

        // Apply same sorting as file list
        const { key, order } = AppState.currentSort;
        images.sort((a, b) => {
            let valA, valB;
            switch (key) {
                case 'name':
                    return a.name.localeCompare(b.name, 'zh-Hans-CN-u-co-pinyin', { numeric: true, sensitivity: 'base' }) * (order === 'asc' ? 1 : -1);
                case 'type':
                    // For gallery, we know they are all images, but extension might differ
                    valA = a.name.split('.').pop().toLowerCase();
                    valB = b.name.split('.').pop().toLowerCase();
                    return valA.localeCompare(valB) * (order === 'asc' ? 1 : -1);
                case 'date':
                    valA = new Date(a.modif_date);
                    valB = new Date(b.modif_date);
                    break;
                case 'size':
                    valA = a.raw_size;
                    valB = b.raw_size;
                    break;
                default: return 0;
            }
            if (valA < valB) return order === 'asc' ? -1 : 1;
            if (valA > valB) return order === 'asc' ? 1 : -1;
            return a.name.localeCompare(b.name);
        });

        this.currentImages = images;
        this.currentIndex = this.currentImages.findIndex(f => f.id === startFileId);
        if (this.currentIndex === -1) this.currentIndex = 0;

        this.renderFilmstrip();
        this.showImage(this.currentIndex);
        this.modal.classList.remove('hidden');
    },

    close() {
        this._showReqSeq += 1;
        this.modal.classList.add('hidden');
        this.imageEl.src = '';
    },

    _getImageMimeType(fileName) {
        const ext = (fileName && fileName.includes('.')) ? fileName.split('.').pop().toLowerCase() : '';
        if (ext === 'png') return 'image/png';
        if (ext === 'gif') return 'image/gif';
        if (ext === 'webp') return 'image/webp';
        return 'image/jpeg';
    },

    nav(direction) {
        let newIndex = this.currentIndex + direction;
        if (newIndex < 0) newIndex = this.currentImages.length - 1; // Loop
        if (newIndex >= this.currentImages.length) newIndex = 0; // Loop
        this.showImage(newIndex);
    },

    async showImage(index) {
        const reqSeq = ++this._showReqSeq;
        this.currentIndex = index;
        const file = this.currentImages[index];
        
        this.filenameEl.textContent = file.name;
        this.spinner.classList.remove('hidden');
        this.imageEl.classList.add('loading');
        
        // Update Filmstrip Highlight
        const thumbs = this.filmstripEl.querySelectorAll('.filmstrip-item');
        thumbs.forEach(t => t.classList.remove('active'));
        if (thumbs[index]) {
            thumbs[index].classList.add('active');
            thumbs[index].scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
        }

        try {
            const result = await ApiService.getPreview(file.id);
            if (reqSeq !== this._showReqSeq || this.modal.classList.contains('hidden')) {
                return;
            }
            if (result.success && result.preview) {
                const mimeType = this._getImageMimeType(file.name);
                this.imageEl.src = `data:${mimeType};base64,${result.preview}`;
            } else {
                // Fallback or error placeholder
                console.warn("Preview load failed for", file.name);
            }
        } catch (e) {
            if (reqSeq === this._showReqSeq) {
                console.error("Error loading preview:", e);
            }
        } finally {
            if (reqSeq === this._showReqSeq) {
                this.spinner.classList.add('hidden');
                this.imageEl.classList.remove('loading');
                if (!this.modal.classList.contains('hidden')) {
                    this.preloadImages(index);
                }
            }
        }
    },

    renderFilmstrip() {
        this.filmstripEl.innerHTML = '';
        this.currentImages.forEach((file, index) => {
            if (!file) return; // Safeguard

            const div = document.createElement('div');
            div.className = 'filmstrip-item';
            
            const img = document.createElement('img');
            img.dataset.id = file.id;
            // Placeholder icon
            img.src = 'img/transfer.png'; 
            
            // 1. Try Cache
            if (AppState.currentThumbnails && AppState.currentThumbnails[file.id]) {
                img.src = `data:image/jpeg;base64,${AppState.currentThumbnails[file.id]}`;
            } 
            // 2. Try DOM (Fallback)
            else {
                const listImg = document.querySelector(`.file-item[data-id="${file.id}"] .grid-thumb-img`);
                if (listImg && !listImg.classList.contains('hidden') && listImg.src.startsWith('data:')) {
                    img.src = listImg.src;
                }
            }

            div.appendChild(img);
            div.addEventListener('click', (e) => {
                e.stopPropagation();
                this.showImage(index);
            });
            this.filmstripEl.appendChild(div);
        });
    },

    preloadImages(currentIndex) {
        const total = this.currentImages.length;
        const preloadCount = Math.min(3, Math.max(0, total - 1));
        const seen = new Set([currentIndex]);
        for (let i = 1; i <= preloadCount; i++) {
            const nextIndex = (currentIndex + i) % total;
            if (seen.has(nextIndex)) continue;
            seen.add(nextIndex);

            const file = this.currentImages[nextIndex];
            // Fire and forget - this populates the backend LRU cache
            if (file && file.id) {
                ApiService.getPreview(file.id).catch(e => console.warn("Preload failed", e));
            }
        }
    }
};