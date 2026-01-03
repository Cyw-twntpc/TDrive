let tdrive_bridge;
let selectedMethod = 'qr';
let hasQrBeenGeneratedSuccessfullyOnce = false; 

document.addEventListener('DOMContentLoaded', () => {
    initWebChannel();
    setupEventListeners();
});

function initWebChannel() {
    if (typeof qt !== 'undefined' && qt.webChannelTransport) {
        new QWebChannel(qt.webChannelTransport, function(channel) {
            window.tdrive_bridge = channel.objects.tdrive_bridge;
            console.log("QWebChannel bridge initialized on login page.");
            
            if (window.tdrive_bridge && window.tdrive_bridge.login_event) {
                window.tdrive_bridge.login_event.connect(on_login_event);
            }
        });
    } else {
        console.warn("QWebChannel object 'qt' not found. Running in UI testing mode.");
    }
}

function setupEventListeners() {
    document.getElementById('btn-minimize').addEventListener('click', () => {
        if (window.tdrive_bridge) window.tdrive_bridge.minimize_window();
    });
    
    document.getElementById('btn-close').addEventListener('click', () => {
        if (window.tdrive_bridge) window.tdrive_bridge.close_window();
    });

    document.getElementById('submitApiBtn').addEventListener('click', submitApiCredentials);
    document.getElementById('proceedBtn').addEventListener('click', proceedWithMethod);
    document.getElementById('submitPhoneBtn').addEventListener('click', submitPhoneNumber);
    document.getElementById('submitCodeBtn').addEventListener('click', submitVerificationCode);
    document.getElementById('submitPasswordBtn').addEventListener('click', submitPassword);
    document.getElementById('centerRefreshQrBtn').addEventListener('click', startQrLogin);

    document.querySelectorAll('.method-btn').forEach(btn => {
        btn.addEventListener('click', (e) => selectMethod(e.currentTarget.dataset.method));
    });

    document.querySelectorAll('[data-target]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const targetScreen = e.currentTarget.dataset.target;
            showScreen(targetScreen);

            if (targetScreen === 'methodScreen' && window.tdrive_bridge) {
                console.log("Returning to method screen, resetting client...");
                window.tdrive_bridge.reset_client_for_new_login_method(result => {
                    if (!result.success) console.error("Failed to reset client on backend.");
                });
            }
        });
    });

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

    let isDragging = false;
    document.addEventListener('mousedown', (e) => {
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

const UIHandler = {
    showInlineError: (inputId, message) => {
        const errorEl = document.getElementById(`${inputId}Error`);
        const inputEl = document.getElementById(inputId);
        if (errorEl) {
            errorEl.textContent = message ? `⚠ ${message}` : '';
            message ? errorEl.classList.add('show') : errorEl.classList.remove('show');
        }
        if (inputEl) {
            if (message) {
                inputEl.classList.add('input-error', 'shake');
                setTimeout(() => {
                    inputEl.classList.remove('shake');
                }, 300);
            } else {
                inputEl.classList.remove('input-error');
            }
        }
    },
    handleBackendError: (response) => {
        alert(response.message || '發生未知錯誤。');
    }
};

function showScreen(screenId) {
    document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
    document.getElementById(screenId)?.classList.add('active');
}

function setLoading(btnId, loading) {
    const btn = document.getElementById(btnId);
    if (btn) btn.classList.toggle('loading', loading);
}

function selectMethod(method) {
    selectedMethod = method;
    document.querySelectorAll('.method-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelector(`.method-btn[data-method="${method}"]`)?.classList.add('active');
}

function showErrorScreen(title, message) {
    document.getElementById('errorTitle').textContent = title;
    document.getElementById('errorMessage').textContent = message;
    showScreen('errorScreen');
}

window.prefill_api_credentials = (apiId, apiHash) => {
    console.log("Prefilling API credentials from expired session.");
    const apiIdInput = document.getElementById('apiId');
    const apiHashInput = document.getElementById('apiHash');
    
    if (apiIdInput && apiHashInput) {
        apiIdInput.value = apiId;
        apiHashInput.value = apiHash;
        console.log("Credentials prefilled. Waiting for user submission.");
    } else {
        console.error("Prefill failed: API input fields not found.");
    }
};

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

function proceedWithMethod() {
    if (selectedMethod === 'qr') {
        hasQrBeenGeneratedSuccessfullyOnce = false;
        startQrLogin();
    }
    showScreen(selectedMethod + 'Screen');
}

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
                overlay.innerHTML = `<button id="centerRefreshQrBtn" class="center-refresh-btn" title="重新產生"><i class="fas fa-sync-alt"></i></button>`;
                qrContainer.appendChild(overlay);
                document.getElementById('centerRefreshQrBtn').addEventListener('click', startQrLogin);

            } else {
                showErrorScreen('QR Code Generation Failed', result.message || 'An unknown error occurred. Please check your network connection or API keys.');
            }
        });
    }
}

function handleQrExpired() {
    const qrContainer = document.getElementById('qrCodeContainer');
    if (qrContainer) {
        qrContainer.classList.add('expired');
    }
}

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
            if (hasQrBeenGeneratedSuccessfullyOnce) {
                handleQrExpired();
            } else {
                const errorMessage = event.error || 'Login failed. Please try again.';
                showErrorScreen('Login Failed', errorMessage);
            }
            break;
    }
}        

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

function loginSuccess() {
    showScreen('successScreen');
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