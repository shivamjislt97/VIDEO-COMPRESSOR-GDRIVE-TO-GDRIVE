import json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

GITHUB_REPO = os.environ.get('GITHUB_REPOSITORY', '')
GH_PAT = os.environ.get('GH_PAT', '')
RECORDS_FILE = 'compression_records.json'

def log(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)

def extract_file_id(url):
    patterns = [
        r'/file/d/([a-zA-Z0-9_-]+)',
        r'id=([a-zA-Z0-9_-]+)',
        r'/open\?id=([a-zA-Z0-9_-]+)',
        r'/d/([a-zA-Z0-9_-]+)',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def load_previous_records():
    if os.path.exists(RECORDS_FILE):
        with open(RECORDS_FILE) as f:
            records = json.load(f)
        log(f'📖 Loaded {len(records)} previous record(s)')
        return records
    log('📖 No previous records, starting fresh')
    return []

def commit_and_push_records(records):
    with open(RECORDS_FILE, 'w') as f:
        json.dump(records, f, indent=2)
    log(f'📝 Records saved to {RECORDS_FILE} ({len(records)} total)')

    if not GH_PAT:
        log('⚠️ GH_PAT not set, skipping commit+push')
        return

    uploaded = sum(1 for r in records if r.get('status') == 'uploaded')
    failed = sum(1 for r in records if r.get('status') == 'failed')

    subprocess.run(['git', 'config', 'user.name', 'Masterslt97'], capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'masterslt97@users.noreply.github.com'], capture_output=True)
    subprocess.run(['git', 'pull', '--rebase', 'origin', 'main'], capture_output=True)
    subprocess.run(['git', 'add', RECORDS_FILE], capture_output=True)
    r = subprocess.run(
        ['git', 'commit', '-m', f'[skip ci] records: {uploaded} uploaded, {failed} failed, {len(records)} total'],
        capture_output=True, text=True
    )
    if r.returncode != 0 and 'nothing to commit' not in r.stderr and 'nothing to commit' not in r.stdout:
        log(f'⚠️ Commit issue: {r.stderr.strip() or r.stdout.strip()}')
        return
    if 'nothing to commit' in r.stderr or 'nothing to commit' in r.stdout:
        return

    subprocess.run(['git', 'push', 'origin', 'main'], capture_output=True)
    log(f'✅ Committed & pushed {RECORDS_FILE} to repo')

def download_video(file_id, url=None):
    log('⬇️ Trying rclone backend copyid...')
    result = subprocess.run(
        ['rclone', 'backend', 'copyid', 'gdrive:', file_id, './downloads/', '-P'],
        capture_output=True, text=True
    )
    files = os.listdir('downloads')
    files = [f for f in files if os.path.isfile(os.path.join('downloads', f))]
    if result.returncode == 0 and files:
        log(f'✅ rclone download success: {files[0]}')
        return os.path.join('downloads', files[0])

    log('⚠️ rclone failed, falling back to gdown...')
    for path in Path('downloads').iterdir():
        path.unlink()
    if not url:
        raise Exception('No URL provided for gdown fallback')
    subprocess.run(['gdown', url, '-O', './downloads/', '--fuzzy', '--remaining-ok'], check=True)
    files = os.listdir('downloads')
    files = [f for f in files if os.path.isfile(os.path.join('downloads', f))]
    if not files:
        raise Exception('No file downloaded via gdown')
    log(f'✅ gdown download success: {files[0]}')
    return os.path.join('downloads', files[0])

def compress_video(inp, out):
    log('🎬 Compressing with FFmpeg (H.265 CRF 28, slow preset)...')
    subprocess.run(
        ['ffmpeg', '-i', inp,
         '-c:v', 'libx265', '-crf', '28', '-preset', 'slow',
         '-tag:v', 'hvc1',
         '-c:a', 'aac', '-b:a', '128k',
         '-y', out],
        check=True
    )

def upload_to_gdrive(local_path, remote_folder):
    log('⬆️ Uploading to GDrive (rclone)...')
    subprocess.run(
        ['rclone', 'copy', local_path, f'gdrive:{remote_folder}/', '-P'],
        check=True
    )

def main():
    os.makedirs('downloads', exist_ok=True)
    os.makedirs('compressed', exist_ok=True)

    records = load_previous_records()
    processed_links = {r['gdrive_link'] for r in records}

    links_json = os.environ.get('GDRIVE_LINKS', '{}')
    try:
        folders = json.loads(links_json)
    except json.JSONDecodeError as e:
        log(f'❌ Invalid GDRIVE_LINKS JSON: {e}')
        sys.exit(1)

    total_videos = sum(len(urls) for urls in folders.values())
    completed = sum(1 for r in records if r.get('status') == 'uploaded')
    log(f'📊 {completed}/{total_videos} videos already processed')

    for folder_name, urls in folders.items():
        for url in urls:
            if url in processed_links:
                log(f'⏩ SKIP (already processed): {url}')
                continue

            log(f'\n{"─" * 60}')
            log(f'📌 Processing: {url}')
            log(f'📂 Folder:     {folder_name}')
            log(f'{"─" * 60}')

            file_id = extract_file_id(url)
            if not file_id:
                log(f'❌ Could not extract file ID from: {url}')
                records.append({
                    'video_name': url,
                    'gdrive_link': url,
                    'folder': folder_name,
                    'status': 'failed',
                    'error': 'Invalid GDrive URL - could not extract file ID',
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
                commit_and_push_records(records)
                continue

            try:
                # Download
                input_path = download_video(file_id, url)
                orig_size = os.path.getsize(input_path)
                log(f'📁 Original: {os.path.basename(input_path)} ({orig_size / 1048576:.2f} MB)')

                # Compress
                base_name = os.path.splitext(os.path.basename(input_path))[0]
                output_path = os.path.join('compressed', f'{base_name}_compressed.mp4')

                compress_video(input_path, output_path)
                comp_size = os.path.getsize(output_path)
                saved = round((1 - comp_size / orig_size) * 100, 1)
                log(f'✅ Compressed: {comp_size / 1048576:.2f} MB (saved {saved}%)')

                # Upload
                upload_to_gdrive(output_path, folder_name)
                log(f'✅ Uploaded to gdrive:{folder_name}/{base_name}_compressed.mp4')

                # Record
                record = {
                    'video_name': os.path.basename(input_path),
                    'original_size_mb': round(orig_size / 1048576, 2),
                    'compressed_size_mb': round(comp_size / 1048576, 2),
                    'saved_percent': f'{saved}%',
                    'folder': folder_name,
                    'gdrive_link': url,
                    'status': 'uploaded',
                    'timestamp': datetime.now(timezone.utc).isoformat()
                }
                records.append(record)

            except subprocess.CalledProcessError as e:
                log(f'❌ Processing failed: {e}')
                records.append({
                    'video_name': os.path.basename(url),
                    'gdrive_link': url,
                    'folder': folder_name,
                    'status': 'failed',
                    'error': str(e),
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
                commit_and_push_records(records)
                continue

            except Exception as e:
                log(f'❌ Unexpected error: {e}')
                records.append({
                    'gdrive_link': url,
                    'folder': folder_name,
                    'status': 'failed',
                    'error': str(e),
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
                commit_and_push_records(records)
                continue

            # Save, commit & push records to repo after each upload
            commit_and_push_records(records)

            # Cleanup for next video
            for path in Path('downloads').iterdir():
                path.unlink()
            for path in Path('compressed').iterdir():
                path.unlink()

            processed_links.add(url)

    # Final save
    if records:
        commit_and_push_records(records)

        uploaded = sum(1 for r in records if r.get('status') == 'uploaded')
        failed = sum(1 for r in records if r.get('status') == 'failed')
        log(f'\n{"=" * 60}')
        log(f'🏁 WORKFLOW COMPLETE')
        log(f'✅ Uploaded: {uploaded}  ❌ Failed: {failed}  📝 Total: {len(records)}')
        log(f'{"=" * 60}')

if __name__ == '__main__':
    main()
