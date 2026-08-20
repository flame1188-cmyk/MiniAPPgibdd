#!/usr/bin/env python3
"""
Script to migrate Excel files from PostgreSQL BYTEA to filesystem.

This script:
1. Reads all records from excel_cache table with file1_bytes/file2_bytes
2. Saves files to /app/data/files/{id}_file1.xlsx and {id}_file2.xlsx
3. Updates database with file paths
4. NULLs out the BYTEA columns

Usage:
    python scripts/migrate_excel_to_disk.py
    
Environment:
    DATABASE_URL must be set in .env or environment
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load environment
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set. Please set it in .env or environment.")
    sys.exit(1)

FILES_DIR = Path("/app/data/files")
BATCH_SIZE = 100


def migrate():
    """Main migration function."""
    import asyncio
    from psycopg import AsyncConnection, sql
    from psycopg.rows import dict_row
    
    async def run_migration():
        print(f"Starting Excel files migration to {FILES_DIR}")
        print(f"Database: {DATABASE_URL[:30]}...")
        
        # Create files directory
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Files directory: {FILES_DIR}")
        
        async with await AsyncConnection.connect(DATABASE_URL) as conn:
            # Check if columns exist
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = 'excel_cache' 
                    AND column_name IN ('file1_bytes', 'file2_bytes', 'file1_path', 'file2_path')
                """)
                columns = [row[0] for row in await cur.fetchall()]
                
                if 'file1_bytes' not in columns:
                    print("✓ file1_bytes column does not exist - migration already done or schema different")
                    return
                
                if 'file1_path' not in columns:
                    print("Adding file1_path and file2_path columns...")
                    await cur.execute("ALTER TABLE excel_cache ADD COLUMN IF NOT EXISTS file1_path TEXT")
                    await cur.execute("ALTER TABLE excel_cache ADD COLUMN IF NOT EXISTS file2_path TEXT")
                    await conn.commit()
                    print("✓ Columns added")
            
            # Count records to migrate
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT COUNT(*) 
                    FROM excel_cache 
                    WHERE file1_bytes IS NOT NULL AND file1_bytes != ''
                """)
                total_count = (await cur.fetchone())[0]
                print(f"Found {total_count} records with Excel files to migrate")
            
            if total_count == 0:
                print("No records to migrate")
                return
            
            # Process in batches
            offset = 0
            migrated_count = 0
            error_count = 0
            
            while offset < total_count:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("""
                        SELECT id, reg_code, dat_hash, file1_bytes, file2_bytes, created_at
                        FROM excel_cache
                        WHERE file1_bytes IS NOT NULL AND file1_bytes != ''
                        ORDER BY id
                        LIMIT %s OFFSET %s
                    """, (BATCH_SIZE, offset))
                    
                    rows = await cur.fetchall()
                    
                    if not rows:
                        break
                    
                    for row in rows:
                        try:
                            record_id = row['id']
                            file1_bytes = bytes(row['file1_bytes']) if row['file1_bytes'] else None
                            file2_bytes = bytes(row['file2_bytes']) if row['file2_bytes'] else None
                            
                            if not file1_bytes or not file2_bytes:
                                print(f"⚠ Record {record_id}: Missing file data, skipping")
                                error_count += 1
                                continue
                            
                            # Generate file paths
                            file1_path = FILES_DIR / f"{record_id}_file1.xlsx"
                            file2_path = FILES_DIR / f"{record_id}_file2.xlsx"
                            
                            # Write files
                            with open(file1_path, 'wb') as f:
                                f.write(file1_bytes)
                            with open(file2_path, 'wb') as f:
                                f.write(file2_bytes)
                            
                            # Update database
                            async with conn.cursor() as update_cur:
                                await update_cur.execute("""
                                    UPDATE excel_cache
                                    SET file1_path = %s,
                                        file2_path = %s,
                                        file1_bytes = NULL,
                                        file2_bytes = NULL
                                    WHERE id = %s
                                """, (str(file1_path), str(file2_path), record_id))
                            
                            migrated_count += 1
                            
                            if migrated_count % 50 == 0:
                                await conn.commit()
                                print(f"Progress: {migrated_count}/{total_count} records migrated")
                        
                        except Exception as e:
                            print(f"ERROR migrating record {row.get('id', 'unknown')}: {e}")
                            error_count += 1
                            await conn.rollback()
                    
                    offset += BATCH_SIZE
                    await conn.commit()
            
            print(f"\n=== Migration Complete ===")
            print(f"Migrated: {migrated_count} records")
            print(f"Errors: {error_count} records")
            print(f"Total disk space used: ~{migrated_count * 1.5} MB (estimated)")
    
    asyncio.run(run_migration())


if __name__ == "__main__":
    migrate()
