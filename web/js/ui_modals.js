/**
 * @fileoverview Provides a simple, Promise-based API for showing standard modal dialogs.
 */
const UIModals = {
    /**
     * Shows a confirmation dialog with OK and Cancel buttons.
     * @param {string} title - The title of the modal.
     * @param {string} message - The message content (can be HTML).
     * @param {string} [okClass='btn-danger'] - The CSS class for the OK button.
     * @returns {Promise<boolean>} A promise that resolves to true if OK is clicked, false otherwise.
     */
    showConfirm(title, message, okClass = 'btn-danger') {
        return new Promise(resolve => {
            const modalId = 'confirm-modal';
            const modal = document.getElementById(modalId);
            modal.querySelector('#confirm-title').textContent = title;
            modal.querySelector('#confirm-message').innerHTML = message;
            const okBtn = modal.querySelector('#confirm-ok-btn');
            okBtn.className = `btn ${okClass}`;

            const onOk = () => { cleanup(); resolve(true); };
            const onClose = () => { cleanup(); resolve(false); };
            
            // Removes event listeners to prevent memory leaks.
            const cleanup = () => {
                UIManager.toggleModal(modalId, false);
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#confirm-cancel-btn').removeEventListener('click', onClose);
                modal.querySelector('#confirm-close-btn').removeEventListener('click', onClose);
            };

            okBtn.addEventListener('click', onOk);
            modal.querySelector('#confirm-cancel-btn').addEventListener('click', onClose);
            modal.querySelector('#confirm-close-btn').addEventListener('click', onClose);
            
            UIManager.toggleModal(modalId, true);
        });
    },
    
    /**
     * Shows an alert dialog with a single OK button.
     * @param {string} title - The title of the modal.
     * @param {string} message - The message content (can be HTML).
     * @param {string} [okClass='btn-primary'] - The CSS class for the OK button.
     * @returns {Promise<boolean>} A promise that resolves to true when the dialog is closed.
     */
    showAlert(title, message, okClass = 'btn-primary') {
        return new Promise(resolve => {
            const modalId = 'alert-modal';
            const modal = document.getElementById(modalId);
            modal.querySelector('#alert-title').textContent = title;
            modal.querySelector('#alert-message').innerHTML = message;
            const okBtn = modal.querySelector('#alert-ok-btn');
            okBtn.className = `btn ${okClass}`;

            const onOk = () => { cleanup(); resolve(true); };
            
            const cleanup = () => {
                UIManager.toggleModal(modalId, false);
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#alert-close-btn').removeEventListener('click', onOk);
            };

            okBtn.addEventListener('click', onOk);
            modal.querySelector('#alert-close-btn').addEventListener('click', onOk);
            
            UIManager.toggleModal(modalId, true);
        });
    },
    
    /**
     * Shows a prompt dialog with a text input field.
     * @param {string} title - The title of the modal.
     * @param {string} message - The message displayed above the input.
     * @param {string} [defaultValue=''] - The default value for the input field.
     * @param {Function|null} [asyncValidator=null] - An optional async function to validate the input value.
     * @returns {Promise<string|null>} A promise that resolves with the input value, or null if cancelled.
     */
    async showPrompt(title, message, defaultValue = '', asyncValidator = null) {
        return new Promise(resolve => {
            const modalId = 'prompt-modal';
            const modal = document.getElementById(modalId);
            const input = modal.querySelector('#prompt-input');
            const errorEl = modal.querySelector('#prompt-error');
            const okBtn = modal.querySelector('#prompt-ok-btn');
            
            modal.querySelector('#prompt-title').textContent = title;
            modal.querySelector('#prompt-message').textContent = message;
            input.value = defaultValue;
            errorEl.classList.add('hidden');
            errorEl.textContent = '';
            okBtn.disabled = false;
            okBtn.classList.remove('loading');

            const onOk = async () => {
                const value = input.value.trim();
                errorEl.classList.add('hidden');

                if (!value) {
                    errorEl.textContent = 'The name cannot be empty.';
                    errorEl.classList.remove('hidden');
                    return;
                }

                if (asyncValidator) {
                    okBtn.classList.add('loading');
                    okBtn.disabled = true;
                    const result = await asyncValidator(value);
                    okBtn.classList.remove('loading');
                    okBtn.disabled = false;

                    if (result && !result.success) {
                        errorEl.textContent = result.message || 'An unknown validation error occurred.';
                        errorEl.classList.remove('hidden');
                        return; // Keep the prompt open
                    }
                }
                
                cleanup();
                resolve(value);
            };

            const onClose = () => { cleanup(); resolve(null); };
            const onKeyPress = (e) => { if (e.key === 'Enter') onOk(); };
            const onInput = () => errorEl.classList.add('hidden');

            const cleanup = () => {
                UIManager.toggleModal(modalId, false);
                input.removeEventListener('keydown', onKeyPress);
                input.removeEventListener('input', onInput);
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#prompt-cancel-btn').removeEventListener('click', onClose);
                modal.querySelector('#prompt-close-btn').removeEventListener('click', onClose);
            };

            input.addEventListener('keydown', onKeyPress);
            input.addEventListener('input', onInput);
            okBtn.addEventListener('click', onOk);
            modal.querySelector('#prompt-cancel-btn').addEventListener('click', onClose);
            modal.querySelector('#prompt-close-btn').addEventListener('click', onClose);

            UIManager.toggleModal(modalId, true);
            input.focus();
            input.select();
        });
    }
};
