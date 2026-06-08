class I18nManager {
    constructor() {
        this.locale = 'zh-TW'; // Default locale
        this.translations = {};
        this.loaded = false;
    }

    async init(defaultLocale = 'zh-TW') {
        this.locale = defaultLocale;
        await this.loadTranslations(this.locale);
        this.translateDOM();
        this.loaded = true;
    }

    async loadTranslations(locale) {
        try {
            // Using a relative path that works from /web/
            const response = await fetch(`locales/${locale}.json`);
            if (!response.ok) {
                console.warn(`Failed to load locale: ${locale}. Falling back to zh-TW.`);
                if (locale !== 'zh-TW') await this.loadTranslations('zh-TW');
                return;
            }
            this.translations = await response.json();
        } catch (error) {
            console.error("i18n loading error:", error);
        }
    }

    t(key) {
        if (!key) return '';
        const keys = key.split('.');
        let current = this.translations;
        for (const k of keys) {
            if (current && current.hasOwnProperty(k)) {
                current = current[k];
            } else {
                return key; // Return the key itself if missing
            }
        }
        return current;
    }

    translateDOM() {
        // Translate text content
        document.querySelectorAll('[data-i18n]').forEach(el => {
            const key = el.getAttribute('data-i18n');
            const translation = this.t(key);
            if (translation !== key) {
                // Determine if we need to set innerHTML or textContent
                if (translation.includes('<')) {
                    el.innerHTML = translation;
                } else {
                    el.textContent = translation;
                }
            }
        });

        // Translate placeholders
        document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
            const key = el.getAttribute('data-i18n-placeholder');
            const translation = this.t(key);
            if (translation !== key) {
                el.setAttribute('placeholder', translation);
            }
        });

        // Translate titles (tooltips)
        document.querySelectorAll('[data-i18n-title]').forEach(el => {
            const key = el.getAttribute('data-i18n-title');
            const translation = this.t(key);
            if (translation !== key) {
                el.setAttribute('title', translation);
            }
        });
    }
}

// Global instance
window.i18n = new I18nManager();
window.t = (key) => window.i18n.t(key);

// Auto initialize on DOM load
document.addEventListener('DOMContentLoaded', () => {
    window.i18n.init().then(() => {
        // Re-translate document title if set
        const titleEl = document.querySelector('title[data-i18n]');
        if (titleEl) {
            document.title = window.t(titleEl.getAttribute('data-i18n'));
        }
    });
});
