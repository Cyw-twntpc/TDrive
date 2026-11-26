import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Optional

from core_app.core.errors import ErrorCode
from core_app.core.settings_manager import SettingsManager
from core_app.infrastructure.database.main_db.database import DatabaseConnection
from core_app.infrastructure.database.main_db.repositories.file_repository import FileRepository

logger = logging.getLogger(__name__)


class PlaylistManager:
    """Manages local user audio playlists persisted as JSON in user_data."""

    def __init__(
        self,
        storage_dir: str = "file/user_data",
        filename: str = "playlists.json",
        db_handler=None,
    ):
        if storage_dir in (
            "file/user_data",
            "./file/user_data",
            os.path.abspath("file/user_data"),
            os.path.abspath("./file/user_data"),
        ):
            resolved_dir = SettingsManager().settings_dir
        else:
            resolved_dir = os.path.abspath(storage_dir)

        self.storage_dir = resolved_dir
        self.playlists_file = os.path.join(self.storage_dir, filename)
        self._db_handler = db_handler
        self._lock = threading.RLock()
        self._ensure_file_exists()

    def _get_db(self):
        return self._db_handler or DatabaseConnection()

    def _ensure_file_exists(self) -> None:
        """Create the local playlist storage file if it does not yet exist."""
        with self._lock:
            os.makedirs(self.storage_dir, exist_ok=True)
            if not os.path.exists(self.playlists_file):
                self._write_data({"playlists": []})

    def _read_data(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_file_exists()
            try:
                with open(self.playlists_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("playlists"), list):
                    return data
            except Exception as exc:
                logger.error("Failed to read playlists file %s: %s", self.playlists_file, exc)
            return {"playlists": []}

    def _write_data(self, data: dict[str, Any]) -> None:
        with self._lock:
            os.makedirs(self.storage_dir, exist_ok=True)
            tmp_path = f"{self.playlists_file}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.playlists_file)

    @staticmethod
    def _summarize(playlist: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": playlist["id"],
            "name": playlist["name"],
            "item_count": len(playlist.get("items", [])),
            "created_at": playlist.get("created_at", 0.0),
            "updated_at": playlist.get("updated_at", 0.0),
        }

    def get_playlists(self) -> list[dict[str, Any]]:
        """Return summary metadata for all locally saved playlists."""
        data = self._read_data()
        return [self._summarize(pl) for pl in data["playlists"]]

    def _build_missing_item(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": entry.get("map_id"),
            "file_id": entry.get("file_id"),
            "file_hash": entry.get("file_hash", ""),
            "name": entry.get("name", "Unknown Track"),
            "parent_id": None,
            "folder_id": None,
            "folder_name": "",
            "folder_path": "",
            "raw_size": 0,
            "size": "-",
            "raw_date": 0,
            "modif_date": "-",
            "added_at": entry.get("added_at", 0.0),
            "missing": True,
        }

    def _reconcile_playlist_items(self, playlist: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """Resolve stored playlist entries against active audio rows using map_id then file_hash."""
        repo = FileRepository(self._get_db())
        all_audio = repo.get_all_audio_files()
        by_map_id = {item["id"]: item for item in all_audio}
        by_hash = {}
        for item in all_audio:
            by_hash.setdefault(item["file_hash"], item)

        resolved_items: list[dict[str, Any]] = []
        dirty = False
        for entry in playlist.get("items", []):
            map_id = entry.get("map_id")
            file_hash = entry.get("file_hash")
            live = by_map_id.get(map_id)
            if live is None and file_hash:
                live = by_hash.get(file_hash)
                if live is not None:
                    # Update stale map_id/name when an audio file was moved or renamed.
                    entry["map_id"] = live["id"]
                    entry["file_id"] = live["file_id"]
                    entry["name"] = live["name"]
                    dirty = True
            elif live is not None and entry.get("name") != live["name"]:
                entry["name"] = live["name"]
                dirty = True

            if live is not None:
                resolved = dict(live)
                resolved["added_at"] = entry.get("added_at", 0.0)
                resolved["missing"] = False
                resolved_items.append(resolved)
            else:
                resolved_items.append(self._build_missing_item(entry))

        return resolved_items, dirty

    def get_playlist_items(self, playlist_id: str) -> dict[str, Any]:
        """Return a playlist's resolved track list against the current active file database."""
        with self._lock:
            data = self._read_data()
            target = next((pl for pl in data["playlists"] if pl["id"] == playlist_id), None)
            if target is None:
                return {"success": False, "error_code": ErrorCode.PATH_NOT_FOUND}

            resolved_items, dirty = self._reconcile_playlist_items(target)
            if dirty:
                target["updated_at"] = time.time()
                self._write_data(data)

            return {
                "success": True,
                "playlist": {
                    "id": target["id"],
                    "name": target["name"],
                    "items": resolved_items,
                },
            }

    def create_playlist(self, name: str) -> dict[str, Any]:
        """Create a new local playlist with a validated unique name."""
        clean_name = (name or "").strip()
        if not clean_name:
            return {"success": False, "error_code": ErrorCode.INVALID_OPERATION}

        with self._lock:
            data = self._read_data()
            if any(pl["name"].strip().lower() == clean_name.lower() for pl in data["playlists"]):
                return {"success": False, "error_code": ErrorCode.ITEM_ALREADY_EXISTS}

            now = time.time()
            new_pl = {
                "id": f"pl_{int(now * 1000)}_{uuid.uuid4().hex[:6]}",
                "name": clean_name,
                "created_at": now,
                "updated_at": now,
                "items": [],
            }
            data["playlists"].append(new_pl)
            self._write_data(data)
            return {"success": True, "playlist": self._summarize(new_pl)}

    def rename_playlist(self, playlist_id: str, new_name: str) -> dict[str, Any]:
        """Rename an existing local playlist while enforcing name uniqueness."""
        clean_name = (new_name or "").strip()
        if not clean_name:
            return {"success": False, "error_code": ErrorCode.INVALID_OPERATION}

        with self._lock:
            data = self._read_data()
            target = next((pl for pl in data["playlists"] if pl["id"] == playlist_id), None)
            if target is None:
                return {"success": False, "error_code": ErrorCode.PATH_NOT_FOUND}

            duplicate = any(
                pl["id"] != playlist_id and pl["name"].strip().lower() == clean_name.lower()
                for pl in data["playlists"]
            )
            if duplicate:
                return {"success": False, "error_code": ErrorCode.ITEM_ALREADY_EXISTS}

            target["name"] = clean_name
            target["updated_at"] = time.time()
            self._write_data(data)
            return {"success": True, "playlist": self._summarize(target)}

    def delete_playlist(self, playlist_id: str) -> dict[str, Any]:
        """Delete a local playlist by ID."""
        with self._lock:
            data = self._read_data()
            original_len = len(data["playlists"])
            data["playlists"] = [pl for pl in data["playlists"] if pl["id"] != playlist_id]
            if len(data["playlists"]) == original_len:
                return {"success": False, "error_code": ErrorCode.PATH_NOT_FOUND}

            self._write_data(data)
            return {"success": True}

    def add_items_to_playlist(self, playlist_id: str, map_ids: list[int]) -> dict[str, Any]:
        """Add one or more cloud audio map IDs to a playlist, skipping duplicates."""
        with self._lock:
            data = self._read_data()
            target = next((pl for pl in data["playlists"] if pl["id"] == playlist_id), None)
            if target is None:
                return {"success": False, "error_code": ErrorCode.PATH_NOT_FOUND}

            repo = FileRepository(self._get_db())
            by_map_id = {item["id"]: item for item in repo.get_all_audio_files()}
            existing_map_ids = {item.get("map_id") for item in target["items"]}
            existing_hashes = {item.get("file_hash") for item in target["items"] if item.get("file_hash")}

            added = 0
            now = time.time()
            for raw_id in map_ids or []:
                try:
                    map_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                audio_info = by_map_id.get(map_id)
                if not audio_info:
                    continue
                if map_id in existing_map_ids or audio_info["file_hash"] in existing_hashes:
                    continue

                target["items"].append({
                    "map_id": map_id,
                    "file_id": audio_info["file_id"],
                    "file_hash": audio_info["file_hash"],
                    "name": audio_info["name"],
                    "added_at": now,
                })
                existing_map_ids.add(map_id)
                existing_hashes.add(audio_info["file_hash"])
                added += 1

            if added > 0:
                target["updated_at"] = now
                self._write_data(data)

            return {"success": True, "added": added, "playlist": self._summarize(target)}

    def remove_item_from_playlist(self, playlist_id: str, map_id: int) -> dict[str, Any]:
        """Remove an audio track from a local playlist by its map ID."""
        with self._lock:
            data = self._read_data()
            target = next((pl for pl in data["playlists"] if pl["id"] == playlist_id), None)
            if target is None:
                return {"success": False, "error_code": ErrorCode.PATH_NOT_FOUND}

            before = len(target["items"])
            target["items"] = [item for item in target["items"] if item.get("map_id") != map_id]
            if len(target["items"]) != before:
                target["updated_at"] = time.time()
                self._write_data(data)

            return {"success": True, "playlist": self._summarize(target)}

    def reorder_playlist_items(self, playlist_id: str, ordered_map_ids: list[int]) -> dict[str, Any]:
        """Reorder tracks in a playlist according to the provided sequence of map IDs."""
        with self._lock:
            data = self._read_data()
            target = next((pl for pl in data["playlists"] if pl["id"] == playlist_id), None)
            if target is None:
                return {"success": False, "error_code": ErrorCode.PATH_NOT_FOUND}

            item_by_id = {item.get("map_id"): item for item in target["items"]}
            reordered: list[dict[str, Any]] = []
            seen: set[Optional[int]] = set()
            for raw_id in ordered_map_ids or []:
                try:
                    mid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if mid in item_by_id and mid not in seen:
                    reordered.append(item_by_id[mid])
                    seen.add(mid)

            # Append any entries not explicitly listed so tracks are never dropped silently.
            for item in target["items"]:
                if item.get("map_id") not in seen:
                    reordered.append(item)

            target["items"] = reordered
            target["updated_at"] = time.time()
            self._write_data(data)
            return {"success": True, "playlist": self._summarize(target)}
