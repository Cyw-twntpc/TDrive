# TDrive: A Secure Cloud Storage Solution Leveraging Telegram's Infrastructure

A high-performance, desktop cloud storage client that utilizes Telegram's unlimited cloud storage as a secure and cost-effective backend.

## Demonstration

*The project features a modern, responsive UI built with Web technologies, hosted within a native Qt application.*

## Tech Stack

-   **Backend:**
    -   **Language:** Python 3
    -   **Core Library:** Telethon (for interacting with the Telegram API)
    -   **Concurrency:** `asyncio` integrated with Qt via `qasync`
    -   **Database:** SQLite (Syncs with cloud)
    -   **Cryptography:** AES for securing local credentials and file hashes

-   **Frontend (UI):**
    -   **Languages:** HTML5, CSS3, JavaScript (ES6+)
    -   **Architecture:** Single Page Application (SPA)
    -   **Icons:** Font Awesome 5

-   **GUI Framework:**
    -   **PySide6 (Qt for Python):** Uses `QWebEngineView` to render the UI and `QWebChannel` for seamless bi-directional communication between Python and JavaScript.

## Key Features

-   **Secure Authentication:** Supports multiple login methods including QR code login (via official Telegram app) and phone number verification.
-   **Full CRUD Operations:** Create folders, rename items, move files/folders, and delete content with changes synced across devices.
-   **Resumable Transfers:** Robust upload and download manager supporting **Pause** and **Resume** functionalities for individual tasks or bulk operations.
-   **Traffic Monitoring:** Real-time dashboard showing daily upload/download traffic usage.
-   **Recursive Folder Operations:** Supports uploading and downloading complex, nested folder structures while preserving hierarchy.
-   **Smart Deduplication:** Implements SHA256 hash-based deduplication to skip uploading files that already exist in the drive, saving bandwidth.
-   **High-Performance Search:** Streaming search results allowing for rapid discovery of files across the remote database.
-   **Native Experience:** System tray integration, native file dialogs, and persistent session management.

## System Architecture & Technical Highlights

This project serves as a **Proof of Concept** exploring the feasibility of using distributed, end-to-end encrypted messaging protocols as a resilient storage layer.

### 1. Hybrid Architecture (Python + Web Tech)
Unlike the previous version which used Eel, this iteration leverages **PySide6** and **QWebEngine**. This provides a more stable, production-grade native window wrapper while keeping the flexibility of web technologies for UI design.
-   **The Bridge:** A `Bridge` class acts as the intermediary, exposing Python methods to the JavaScript context via `QWebChannel`.
-   **Facade Pattern:** The backend logic is encapsulated in a `TDriveService` facade, ensuring a clean separation of concerns between the UI logic and the core business services (`Auth`, `File`, `Transfer`).

### 2. Asyncio & Qt Integration
Integrating Python's `asyncio` library with Qt's event loop is critical for a responsive UI.
-   **Solution:** The project uses `qasync` to run the `asyncio` event loop on top of the Qt event loop. This allows non-blocking network operations (Telethon calls) to coexist with GUI updates on the same thread, eliminating common freezing issues found in synchronous GUI apps.

### 3. Advanced Transfer Engine
The transfer system has been completely refactored for reliability and performance.
-   **Throttling & UI Performance:** High-speed transfers generate thousands of progress events per second. The backend implements a smart throttling mechanism (limiting updates to ~30ms) to prevent flooding the UI thread, ensuring smooth animations even during heavy loads.
-   **State Machine:** A centralized `TransferController` manages the state of all tasks, handling complex scenarios like resuming interrupted folder uploads or cleaning up partial data upon cancellation.
-   **Optimistic UI:** The frontend (`TransferManager.js` & `ActionHandler.js`) implements optimistic updates for immediate visual feedback while the backend processes the request.

## How to Run

1.  **Prerequisites:** Python 3.9+ installed.
2.  **Clone/Download:** Obtain the project files.
3.  **Setup Virtual Environment:**
    ```bash
    python -m venv venv
    # Windows
    venv\Scripts\activate
    # Linux/Mac
    source venv/bin/activate
    ```
4.  **Install Dependencies:**
    ```bash
    pip install PySide6 telethon qasync qrcode-art
    ```
5.  **Run the Application:**
    ```bash
    python main.py
    ```

## Future Work

-   **Global Search & Indexing:** Improve search capabilities with a local full-text search index.
-   **File Sharing:** Implement a mechanism to share files publicly via Telegram links.
-   **Theme Customization:** Add full support for dark/light mode switching.
-   **CI/CD:** Automated builds for Windows (.exe) and macOS (.app).
