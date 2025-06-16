#!/usr/bin/env python3
"""
Script to upload a PDF to the admin account and duplicate it to all other users.

Usage:
    python add_pdf_to_all_users.py <path_to_pdf>
"""

import sys
import time
import requests
import argparse
from pathlib import Path
from typing import List, Dict, Optional


def get_users(api_base_url: str) -> List[Dict]:
    """Get all users from the API."""
    response = requests.get(f"{api_base_url}/users/")
    response.raise_for_status()
    return response.json()["users"]


def find_admin_user(users: List[Dict]) -> Optional[str]:
    """Find the admin user ID from the list of users."""
    for user in users:
        if user.get("admin", False):
            return user["id"]
    return None


def upload_file(api_base_url: str, pdf_path: Path, user_id: str) -> str:
    """Upload a PDF file to the specified user and return the file ID."""
    with open(pdf_path, "rb") as f:
        files = {"file": (pdf_path.name, f, "application/pdf")}
        data = {"user_id": user_id}
        
        response = requests.post(f"{api_base_url}/upload/", files=files, data=data)
        response.raise_for_status()
        
        result = response.json()
        return result["file_id"]


def duplicate_file(api_base_url: str, file_id: str, original_user_id: str, new_user_id: str) -> str:
    """Duplicate a file from one user to another."""
    data = {
        "original_user_id": original_user_id,
        "new_user_id": new_user_id
    }
    
    response = requests.post(f"{api_base_url}/files/{file_id}/duplicate/", data=data)
    response.raise_for_status()
    
    result = response.json()
    return result["new_file_id"]


def main():
    parser = argparse.ArgumentParser(description="Upload PDF to admin and duplicate to all users")
    parser.add_argument("pdf_path", help="Path to the PDF file to upload")
    parser.add_argument("--api-url", default="http://localhost:7860/dokumentation", 
                       help="Base URL of the FastAPI server (default: http://localhost:7860/dokumentation)")
    parser.add_argument("--wait-time", type=int, default=20,
                       help="Wait time in seconds after upload before duplicating (default: 20)")
    
    args = parser.parse_args()
    
    # Validate PDF file
    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"Error: PDF file '{pdf_path}' does not exist")
        sys.exit(1)
    
    if not pdf_path.suffix.lower() == ".pdf":
        print(f"Error: File '{pdf_path}' is not a PDF file")
        sys.exit(1)
    
    api_base_url = args.api_url.rstrip("/")
    
    try:
        # Step 1: Get all users
        print("Fetching users...")
        users = get_users(api_base_url)
        print(f"Found {len(users)} users")
        
        # Step 2: Find admin user
        admin_user_id = find_admin_user(users)
        if not admin_user_id:
            print("Error: No admin user found")
            sys.exit(1)
        
        admin_user = next(u for u in users if u["id"] == admin_user_id)
        print(f"Found admin user: {admin_user['username']} (ID: {admin_user_id})")
        
        # Step 3: Upload PDF to admin account
        print(f"Uploading '{pdf_path.name}' to admin account...")
        file_id = upload_file(api_base_url, pdf_path, admin_user_id)
        print(f"Upload successful! File ID: {file_id}")
        
        # Step 4: Wait for processing
        print(f"Waiting {args.wait_time} seconds for processing to complete...")
        time.sleep(args.wait_time)
        
        # Step 5: Duplicate to all other users
        other_users = [u for u in users if u["id"] != admin_user_id]
        print(f"Duplicating file to {len(other_users)} other users...")
        
        success_count = 0
        for user in other_users:
            try:
                new_file_id = duplicate_file(api_base_url, file_id, admin_user_id, user["id"])
                print(f"✓ Duplicated to {user['username']} (new file ID: {new_file_id})")
                success_count += 1
            except requests.RequestException as e:
                print(f"✗ Failed to duplicate to {user['username']}: {e}")
        
        print(f"\nCompleted! Successfully duplicated to {success_count}/{len(other_users)} users")
        
    except requests.RequestException as e:
        print(f"API Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()