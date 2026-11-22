"""
Comprehensive unit tests for robot storage manager core functionality.
"""
import os
import pytest
from pathlib import Path
from datetime import datetime, timedelta

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from robot_storage_manager.core import (
    scan_for_data_files,
    scan_for_manifests,
    load_or_create_manifest,
    save_manifest,
    RetentionPlanner,
)


class TestScanForDataFiles:
    """Test file scanning functionality."""

    def test_scan_nested_directories(self, tmp_path):
        """Test scanning works recursively through subdirectories."""
        # Create nested structure
        sub1 = tmp_path / "sub1"
        sub2 = tmp_path / "sub1" / "sub2"
        sub1.mkdir()
        sub2.mkdir()
        
        (tmp_path / "root.mcap").write_text("root")
        (sub1 / "sub1.mcap").write_text("sub1")
        (sub2 / "sub2.mcap").write_text("sub2")
        
        files = scan_for_data_files(tmp_path, extension=".mcap")
        assert len(files) == 3
        
        # Check files are sorted by path
        file_names = [f.name for f in files]
        assert "root.mcap" in file_names
        assert "sub1.mcap" in file_names
        assert "sub2.mcap" in file_names

    def test_scan_rejects_file_symlink_that_escapes_root(self, tmp_path):
        capture_root = tmp_path / "capture"
        capture_root.mkdir()
        outside = tmp_path / "outside.mcap"
        outside.write_bytes(b"do not touch")
        (capture_root / "linked.mcap").symlink_to(outside)

        assert scan_for_data_files(capture_root) == []

    def test_manifest_scan_rejects_symlink_that_escapes_root(self, tmp_path):
        capture_root = tmp_path / "capture"
        capture_root.mkdir()
        outside = tmp_path / "outside.mcap.manifest.json"
        outside.write_text('{"status":"completed"}')
        (capture_root / "linked.mcap.manifest.json").symlink_to(outside)

        assert scan_for_manifests(capture_root) == []


class TestRetentionPlanner:
    """Test retention planning functionality."""
    
    def test_retention_by_age(self, tmp_path):
        """Test retention planner removes old files."""
        # Create files with different ages
        now = datetime.now()
        
        old_file = tmp_path / "old.mcap"
        old_file.write_bytes(b"x" * 5 * 1024 * 1024)  # 5MB
        os.utime(old_file, times=((now - timedelta(days=20)).timestamp(),) * 2)
        
        new_file = tmp_path / "new.mcap"
        new_file.write_bytes(b"x" * 5 * 1024 * 1024)  # 5MB
        
        # Create manifests for the files
        old_manifest = load_or_create_manifest(old_file)
        old_manifest["status"] = "completed"
        old_manifest["sha256_hash"] = "a" * 64
        old_manifest["upload_id"] = "upload-old"
        save_manifest(old_manifest)
        new_manifest = load_or_create_manifest(new_file)
        new_manifest["status"] = "completed"
        new_manifest["sha256_hash"] = "b" * 64
        new_manifest["upload_id"] = "upload-new"
        save_manifest(new_manifest)
        
        # Planner with very small size limit to trigger deletion
        planner = RetentionPlanner(min_retain_days=15, max_total_gib=0.008)  # ~8MB limit
        
        plan = planner.plan([old_manifest, new_manifest])
        
        assert "to_delete" in plan
        assert len(plan["to_delete"]) >= 1
        # The old file should be prioritized for deletion
        deleted_paths = [d["path"] for d in plan["to_delete"]]
        assert str(old_file) in deleted_paths
    
    def test_retention_by_size(self, tmp_path):
        """Test retention planner enforces size limits."""
        # Create multiple files
        manifests = []
        for i in range(5):
            f = tmp_path / f"file{i}.mcap"
            # Each file is 1KB
            f.write_bytes(b"x" * 1024)
            # Simulate different ages
            os.utime(f, times=((datetime.now() - timedelta(days=10+i)).timestamp(),) * 2)
            manifest = load_or_create_manifest(f)
            manifest["status"] = "completed"
            manifest["sha256_hash"] = f"{i:x}" * 64
            manifest["upload_id"] = f"upload-{i}"
            save_manifest(manifest)
            manifests.append(manifest)
        
        # Policy allows only 3KB (3/1024 MB = 3/1024/1024 GB)
        planner = RetentionPlanner(min_retain_days=0, max_total_gib=3/1024/1024)
        
        plan = planner.plan(manifests)
        
        # Should delete oldest files to stay under 3KB
        assert "to_delete" in plan
        assert len(plan["to_delete"]) >= 2
    
    def test_retention_respects_min_days(self, tmp_path):
        """Test retention keeps files within min_retain_days even if over size."""
        now = datetime.now()
        
        # Create large recent file
        recent_large = tmp_path / "recent.mcap"
        recent_large.write_bytes(b"x" * 10240)  # 10KB
        
        # Create small old file
        old_small = tmp_path / "old.mcap"
        old_small.write_bytes(b"x" * 1024)  # 1KB
        os.utime(old_small, times=((now - timedelta(days=20)).timestamp(),) * 2)
        
        recent_manifest = load_or_create_manifest(recent_large)
        recent_manifest["status"] = "completed"
        recent_manifest["sha256_hash"] = "c" * 64
        recent_manifest["upload_id"] = "upload-recent"
        save_manifest(recent_manifest)
        old_manifest = load_or_create_manifest(old_small)
        old_manifest["status"] = "completed"
        old_manifest["sha256_hash"] = "d" * 64
        old_manifest["upload_id"] = "upload-old"
        save_manifest(old_manifest)
        
        planner = RetentionPlanner(min_retain_days=5, max_total_gib=5/1024/1024)  # 5KB limit
        
        plan = planner.plan([recent_manifest, old_manifest])
        
        # Should only delete old file, keep recent even though it exceeds size
        assert "to_delete" in plan
        assert len(plan["to_delete"]) == 1
        assert plan["to_delete"][0]["path"] == str(old_small)


class TestErrorHandling:
    """Test error handling in storage operations."""
    
    def test_scan_nonexistent_directory(self):
        """Test scanning handles nonexistent directory gracefully."""
        files = scan_for_data_files(Path("/nonexistent/path"))
        assert len(files) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
