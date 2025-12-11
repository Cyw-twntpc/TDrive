/**
 * @fileoverview Handles all UI logic and user interactions on the login page.
 */

// --- Global State ---
let tdrive_bridge;
let selectedMethod = 'qr';
// This flag helps differentiate between a QR code that expired vs. one that failed to generate initially.
let hasQrBeenGeneratedSuccessfullyOnce = false; 

/**
 * Main initialization function, called when the DOM is fully loaded.
 */
document.addEventListener('DOMContentLoaded', () => {
    initWebChannel();
    setupEventListeners();
});

/**
 * Initializes the QWebChannel to communicate with the Python backend.
 */
function initWebChannel() {
    if (typeof qt !== 'undefined' && qt.webChannelTransport) {
        new QWebChannel(qt.webChannelTransport, function(channel) {
            window.tdrive_bridge = channel.objects.tdrive_bridge;
            console.log("QWebChannel bridge initialized on login page.");
            
            // Connect to backend signals, like real-time login events for QR code scanning.
            if (window.tdrive_bridge && window.tdrive_bridge.login_event) {
                window.tdrive_bridge.login_event.connect(on_login_event);
            }
        });
    } else {
        console.warn("QWebChannel object 'qt' not found. Running in UI testing mode.");
    }
}

/**
 * Sets up all event listeners for the interactive elements on the page.
 */
function setupEventListeners() {
    // --- Window Controls ---
    document.getElementById('btn-minimize').addEventListener('click', () => {
        if (window.tdrive_bridge) window.tdrive_bridge.minimize_window();
    });
    
    document.getElementById('btn-close').addEventListener('click', () => {
        if (window.tdrive_bridge) window.tdrive_bridge.close_window();
    });

    // --- Primary Actions ---
    document.getElementById('submitApiBtn').addEventListener('click', submitApiCredentials);
    document.getElementById('proceedBtn').addEventListener('click', proceedWithMethod);
    document.getElementById('submitPhoneBtn').addEventListener('click', submitPhoneNumber);
    document.getElementById('submitCodeBtn').addEventListener('click', submitVerificationCode);
    document.getElementById('submitPasswordBtn').addEventListener('click', submitPassword);
    document.getElementById('centerRefreshQrBtn').addEventListener('click', startQrLogin);

    // --- Method Selection ---
    document.querySelectorAll('.method-btn').forEach(btn => {
        btn.addEventListener('click', (e) => selectMethod(e.currentTarget.dataset.method));
    });

    // --- Screen Navigation (e.g., 'Back' buttons) ---
    document.querySelectorAll('[data-target]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const targetScreen = e.currentTarget.dataset.target;
            showScreen(targetScreen);

            // When returning to the method selection, reset the backend client to ensure a clean state.
            if (targetScreen === 'methodScreen' && window.tdrive_bridge) {
                console.log("Returning to method screen, resetting client...");
                window.tdrive_bridge.reset_client_for_new_login_method(result => {
                    if (!result.success) console.error("Failed to reset client on backend.");
                });
            }
        });
    });

    // --- Enter Key Support for Input Fields ---
    const addEnterSupport = (inputId, btnId) => {
        const inputElement = document.getElementById(inputId);
        if (inputElement) {
            inputElement.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') document.getElementById(btnId).click();
            });
        }
    };
    addEnterSupport('apiId', 'submitApiBtn');
    addEnterSupport('apiHash', 'submitApiBtn');
    addEnterSupport('phoneNumber', 'submitPhoneBtn');
    addEnterSupport('verificationCode', 'submitCodeBtn');
    addEnterSupport('password', 'submitPasswordBtn');

    // --- Frameless Window Dragging Logic ---
    let isDragging = false;
    document.addEventListener('mousedown', (e) => {
        // Only start dragging if the mousedown is within the designated drag area.
        const dragArea = document.querySelector('.window-drag-area');
        if (dragArea && dragArea.contains(e.target)) {
            isDragging = true;
            if (window.tdrive_bridge) {
                window.tdrive_bridge.handle_drag_start(e.screenX, e.screenY);
            }
            e.preventDefault();
        }
    });
    
    document.addEventListener('mousemove', (e) => {
        if (isDragging && window.tdrive_bridge) {
            window.tdrive_bridge.handle_drag_move(e.screenX, e.screenY);
        }
    });
    
    document.addEventListener('mouseup', () => {
        if (isDragging) {
            isDragging = false;
            if (window.tdrive_bridge) {
                window.tdrive_bridge.handle_drag_end();
            }
        }
    });
}

// --- UI Helper Functions ---

const UIHandler = {
    /**
     * Displays an inline error message for a specific input field.
     * @param {string} inputId - The ID of the input element.
     * @param {string} message - The error message to display.
     */
    showInlineError: (inputId, message) => {
        const errorEl = document.getElementById(`${inputId}Error`);
        const inputEl = document.getElementById(inputId);
        if (errorEl) {
            errorEl.textContent = message ? `⚠ ${message}` : '';
            message ? errorEl.classList.add('show') : errorEl.classList.remove('show');
        }
        if (inputEl) {
            inputEl.classList.toggle('error', !!message);
        }
    },
    /**
     * Displays a generic alert for backend errors.
     * @param {object} response - The error response from the backend.
     */
    handleBackendError: (response) => {
        alert(response.message || '發生未知錯誤。');
    }
};

/**
 * Shows a specific screen by ID and hides all others.
 * @param {string} screenId - The ID of the screen element to show.
 */
function showScreen(screenId) {
    document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
    document.getElementById(screenId)?.classList.add('active');
}

/**
 * Toggles a loading state on a button.
 * @param {string} btnId - The ID of the button element.
 * @param {boolean} loading - True to show the loading spinner, false to hide it.
 */
function setLoading(btnId, loading) {
    const btn = document.getElementById(btnId);
    if (btn) btn.classList.toggle('loading', loading);
}

/**
 * Updates the UI to reflect the selected login method.
 * @param {string} method - The selected method ('qr' or 'phone').
 */
function selectMethod(method) {
    selectedMethod = method;
    document.querySelectorAll('.method-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelector(`.method-btn[data-method="${method}"]`)?.classList.add('active');
}

/**
 * Displays a generic error screen.
 * @param {string} title - The title of the error.
 * @param {string} message - The detailed error message.
 */
function showErrorScreen(title, message) {
    document.getElementById('errorTitle').textContent = title;
    document.getElementById('errorMessage').textContent = message;
    showScreen('errorScreen');
}

/**
 * Called by the Python backend to pre-fill API credentials if a session has expired.
 * @param {string} apiId - The user's API ID.
 * @param {string} apiHash - The user's API Hash.
 */
window.prefill_api_credentials = (apiId, apiHash) => {
    console.log("Prefilling API credentials from expired session.");
    const apiIdInput = document.getElementById('apiId');
    const apiHashInput = document.getElementById('apiHash');
    
    if (apiIdInput && apiHashInput) {
        apiIdInput.value = apiId;
        apiHashInput.value = apiHash;
        // User is expected to manually submit the credentials.
        console.log("Credentials prefilled. Waiting for user submission.");
    } else {
        console.error("Prefill failed: API input fields not found.");
    }
};

// --- Business Logic Functions ---

/**
 * Validates and submits API credentials to the backend.
 */
function submitApiCredentials() {
    ['apiId', 'apiHash'].forEach(id => UIHandler.showInlineError(id, ''));
    const apiId = document.getElementById('apiId').value.trim();
    const apiHash = document.getElementById('apiHash').value.trim();
    let hasError = false;

    if (!apiId || !/^\d+$/.test(apiId)) { UIHandler.showInlineError('apiId', '請輸入有效的 API ID (僅限數字)'); hasError = true; }
    if (!apiHash) { UIHandler.showInlineError('apiHash', '請輸入 API Hash'); hasError = true; }
    if (hasError) return;

    setLoading('submitApiBtn', true);
    if(window.tdrive_bridge) {
        window.tdrive_bridge.verify_api_credentials(Number(apiId), apiHash, function(result) {
            setLoading('submitApiBtn', false);
            if (result.success) {
                showScreen('methodScreen');
            } else {
                UIHandler.showInlineError('apiId', result.message);
            }
        });
    }
}

/**
 * Proceeds to the screen for the currently selected login method.
 */
function proceedWithMethod() {
    if (selectedMethod === 'qr') {
        hasQrBeenGeneratedSuccessfullyOnce = false;
        startQrLogin();
    }
    showScreen(selectedMethod + 'Screen');
}

/**
 * Initiates the QR code login process.
 */
function startQrLogin() {
    const qrContainer = document.getElementById('qrCodeContainer');
    qrContainer.classList.remove('expired');
    qrContainer.innerHTML = `<div class="qr-loading"><i class="fas fa-spinner fa-spin"></i><span>產生中...</span></div>
                             <div class="qr-overlay" id="qrOverlay"><button id="centerRefreshQrBtn" class="center-refresh-btn" title="重新產生"><i class="fas fa-sync-alt"></i></button></div>`;
    document.getElementById('centerRefreshQrBtn').addEventListener('click', startQrLogin);
    
    if(window.tdrive_bridge) {
        window.tdrive_bridge.start_qr_login(function(result) {
            if (result.success) {
                hasQrBeenGeneratedSuccessfullyOnce = true;
                const img = document.createElement('img');
                img.src = result.qr_url;
                qrContainer.innerHTML = '';
                qrContainer.appendChild(img);
                
                const overlay = document.createElement('div');
                overlay.className = 'qr-overlay';
                overlay.innerHTML = `<button id="centerRefreshQrBtn" class="center-refresh-btn" title="Regenerate"><i class="fas fa-sync-alt"></i></button>`;
                qrContainer.appendChild(overlay);
                document.getElementById('centerRefreshQrBtn').addEventListener('click', startQrLogin);

            } else {
                showErrorScreen('QR Code Generation Failed', result.message || 'An unknown error occurred. Please check your network connection or API keys.');
            }
        });
    }
}

/**
 * Displays the 'expired' overlay on the QR code.
 */
function handleQrExpired() {
    const qrContainer = document.getElementById('qrCodeContainer');
    if (qrContainer) {
        qrContainer.classList.add('expired');
    }
}

/**
 * Handles real-time login events from the backend (e.g., for QR code status).
 * @param {object} event - The event object from the backend.
 */
function on_login_event(event) {
    console.log('Login Event Received:', event);
    switch (event.status) {
        case 'completed': 
            loginSuccess(); 
            break;
        case 'password_needed': 
            showScreen('passwordScreen'); 
            break;
        case 'failed':
        default:
            // If a QR code was previously generated successfully, this failure is likely an expiration.
            // Otherwise, it's a genuine failure to generate the code in the first place.
            if (hasQrBeenGeneratedSuccessfullyOnce) {
                handleQrExpired();
            } else {
                const errorMessage = event.error || 'Login failed. Please try again.';
                showErrorScreen('Login Failed', errorMessage);
            }
            break;
    }
}        

/**
 * Submits the user's phone number to request a verification code.
 */
function submitPhoneNumber() {
    UIHandler.showInlineError('phoneNumber', '');
    const phone = document.getElementById('phoneNumber').value.trim();
    if (!phone) { UIHandler.showInlineError('phoneNumber', 'Please enter your phone number'); return; }

    setLoading('submitPhoneBtn', true);
    if(window.tdrive_bridge) {
        window.tdrive_bridge.send_code_request(phone, function(result) {
            setLoading('submitPhoneBtn', false);
            if (result.success) {
                document.getElementById('verificationMessage').textContent = `A code has been sent to ${phone}`;
                showScreen('verificationScreen');
            } else {
                UIHandler.showInlineError('phoneNumber', result.message);
            }
        });
    }
}

/**
 * Submits the verification code entered by the user.
 */
function submitVerificationCode() {
    UIHandler.showInlineError('verificationCode', '');
    const code = document.getElementById('verificationCode').value.trim();
    if (!code) { UIHandler.showInlineError('verificationCode', 'Please enter the verification code'); return; }

    setLoading('submitCodeBtn', true);
    if(window.tdrive_bridge) {
        window.tdrive_bridge.submit_verification_code(code, function(result) {
            setLoading('submitCodeBtn', false);
            if (result.success) {
                result.password_needed ? showScreen('passwordScreen') : loginSuccess();
            } else {
                UIHandler.showInlineError('verificationCode', result.message);
            }
        });
    }
}

/**
 * Submits the user's two-factor authentication password.
 */
function submitPassword() {
    UIHandler.showInlineError('password', '');
    const password = document.getElementById('password').value.trim();
    if (!password) { UIHandler.showInlineError('password', 'Please enter your password'); return; }
    
    setLoading('submitPasswordBtn', true);
    if(window.tdrive_bridge) {
        window.tdrive_bridge.submit_password(password, function(result) {
            setLoading('submitPasswordBtn', false);
            if (result.success) {
                loginSuccess();
            } else {
                UIHandler.showInlineError('password', result.message);
            }
        });
    }
}

/**
 * Handles the final steps after a successful login.
 */
function loginSuccess() {
    showScreen('successScreen');
    // Perform post-login tasks (e.g., database sync) and then notify the backend
    // that the UI is ready to switch to the main window.
    setTimeout(() => {
        if(window.tdrive_bridge) {
            window.tdrive_bridge.perform_post_login_initialization(function(result) {
                if (result.success) {
                    window.tdrive_bridge.notify_login_complete();
                } else {
                    UIHandler.handleBackendError(result);
                    showScreen('methodScreen');
                }
            });
        }
    }, 500);
}