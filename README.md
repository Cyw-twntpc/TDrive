# TDrive: A Secure Cloud Storage Solution Leveraging Telegram's Infrastructure

A high-performance, desktop cloud storage client that utilizes Telegram's unlimited cloud storage as a secure and cost-effective backend.

## Demonstration

Here are some screenshots of the TDrive user interface, showcasing the main file browser, the hierarchical transfer manager, and the login screen.


## Tech Stack

-   **Backend:**
    -   **Language:** Python 3
    -   **Core Library:** Telethon (for interacting with the Telegram API)
    -   **Concurrency:** `asyncio`, `threading`
    -   **Database:** SQLite
    -   **Cryptography:** AES for securing local credentials

-   **Frontend:**
    -   **Languages:** HTML5, CSS3, JavaScript (ES6+)
    -   **Framework:** None (Vanilla JS)
    -   **Icons:** Font Awesome

-   **GUI Framework:**
    -   **Eel:** A lightweight Python library for creating Electron-like offline HTML/JS GUI apps.

## Key Features

-   **Secure Authentication:** Multiple login methods including QR code, phone number with verification code, and two-step verification (password). API credentials are encrypted and stored locally.
-   **Full CRUD Operations:** Create, rename, and delete files and folders.
-   **Concurrent Transfers:** Upload and download multiple items simultaneously with a user-configurable concurrency limit.
-   **Advanced Transfer Management:** A detailed transfer manager UI to monitor, cancel, and view the progress of individual and hierarchical transfers.
-   **Hierarchical Folder Operations:** Supports recursive downloading of entire folder structures.
-   **Efficient Uploads:** Implements file hash-based deduplication to avoid re-uploading identical files, saving bandwidth and time.
-   **File System Navigation:** A responsive interface with a file tree, breadcrumb navigation, and a detailed file list.
-   **Powerful Search:** Search for files and folders across the entire drive or within the current folder.
-   **Robust UI:** Supports multi-select, drag-to-select, and sortable columns for an intuitive user experience.

## System Architecture & Technical Highlights

This project serves as a **Proof of Concept** exploring the feasibility of using distributed, end-to-end encrypted messaging protocols as a resilient, albeit unconventional, storage layer. The following architecture was designed for this technical exploration and is not intended to encourage the misuse of any platform's services.

The project is architected as a decoupled system where the Python backend handles all business logic and communication with the Telegram API, while a Vanilla JS frontend serves as a rich, interactive user interface. The `eel` library acts as the bridge between them.

### 1. Asynchronous Core in a Synchronous GUI Framework

-   **Challenge:** The core of the application relies on `telethon`, an `asyncio`-based library, for all network operations. However, the GUI framework and file dialogs operate in a standard synchronous, multi-threaded environment. Integrating these two paradigms without blocking the UI was a primary challenge.
-   **Solution:** I implemented a robust concurrency model where an `asyncio` event loop runs in a dedicated background daemon thread. A thread-safe utility function (`run_coroutine_threadsafe`) is used to submit coroutines from the main thread to the event loop and wait for their results. For long-running tasks like transfers, tasks are created and scheduled on the loop without being awaited, allowing the UI to remain fully responsive.

### 2. High-Performance, Hierarchical Transfer System

-   **Challenge:** Designing a transfer manager that can handle concurrent, cancellable, and hierarchical (folder) operations while providing real-time, granular feedback to the UI was the most complex feature to implement.
-   **Solution:**
    -   **Concurrency Control:** An `asyncio.Semaphore` is used to strictly limit the number of concurrent uploads or downloads, preventing API rate-limiting issues and managing resource usage.
    -   **Hierarchical Task Management:** When a folder download is initiated, the system first recursively fetches the entire file tree from the local database. It then creates a main "parent" task for the folder and individual "child" tasks for each file within it. The frontend UI subscribes to progress updates for both parent and child tasks, allowing it to render a nested progress view. The parent task's progress is dynamically aggregated from its children.
    -   **Graceful Cancellation:** Transfer tasks are stored in a shared dictionary. The `cancel_transfer` function retrieves a task and calls its `cancel()` method. The running coroutine catches the `asyncio.CancelledError` to perform a graceful shutdown of that specific transfer.

### 3. Data Integrity and Deduplication

-   **Challenge:** How to efficiently store file metadata and ensure that large files are uploaded reliably and without duplication.
-   **Solution:**
    -   **Telegram as a Key-Value Store:** The application uses a private Telegram channel as a block storage backend. Large files are split into chunks, uploaded as individual messages, and their message IDs are stored. A central SQLite database file, which contains all file system metadata (names, hierarchy, chunk locations), is itself uploaded to the channel after every modification, acting as a single source of truth that can be synced on startup.
    -   **Content-Defined Deduplication:** Before any upload, the file's SHA256 hash is calculated. If this hash already exists in the database, the system forgoes the upload entirely. Instead, it creates a new metadata entry that points to the existing set of message IDs, effectively creating an instant "copy" without using additional storage or bandwidth.

## How to Run

1.  **Prerequisites:** Python 3.9+ installed.
2.  **Clone/Download:** Obtain the project files.
3.  **Setup Virtual Environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    ```
4.  **Install Dependencies:**
    ```bash
    pip install eel telethon qrcode-art
    ```
5.  **Run the Application:**
    ```bash
    python gui_main.py
    ```

## Future Work

-   **Implement Dark Mode:** The UI framework is ready for a dark theme, which can be implemented for better user comfort.
-   **Global Pause/Resume:** Add functionality to pause and resume all transfers at once, not just individual items.
-   **Comprehensive Test Suite:** Develop a suite of unit and integration tests to ensure long-term stability and prevent regressions.
-   **Standalone Executable:** Package the application using a tool like PyInstaller to create a single-file executable for easy distribution on Windows, macOS, and Linux.
