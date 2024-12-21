import functools
import pathlib
import asyncio
import aiohttp
import aiofiles
import openai
import heapq

from easypy.units import MiB
from io import BytesIO
import concurrent.futures
from datetime import datetime, timedelta
from pydub import AudioSegment
from moviepy.editor import VideoFileClip
from flask import Flask, render_template, jsonify, request, send_file

TEN_MINUTES = 10 * 60
TMP_MEDIA_FILE = "tmp_media_file.mp3"

app = Flask(__name__)
app.config.from_object('config')
client = openai.OpenAI(api_key=app.config['OPENAI_API_KEY'])

@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

@app.route('/')
def home():
    return render_template('home.html')

async def async_write_audiofile(filename, audio_clip):
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        await loop.run_in_executor(pool, audio_clip.write_audiofile, filename)

async def async_transcript(path, dnl_format, language):
    async with aiofiles.open(path, 'rb') as tmp_file:
        try:
            form_data = aiohttp.FormData()
            form_data.add_field(
                "file", tmp_file, filename=pathlib.Path(path).name, content_type='multipart/form-data')
            for key, value in {
                "model": "whisper-1",
                "language": language,
                "response_format": dnl_format,
            }.items():
                form_data.add_field(key, value)
            async with aiohttp.ClientSession() as session:
                response = await session.post(
                    'https://api.openai.com/v1/audio/transcriptions',
                    headers={
                        'Authorization': f'Bearer {client.api_key}',
                    },
                    data=form_data,
                )
                response.raise_for_status()
                return await response.text()
        except aiohttp.ClientError as e:
            return jsonify({'error': f'Error processing audio: {e}'})

@app.route('/transcribe', methods=['POST'])
async def transcribe():
    chunks_heap = []

    async def _process_chunk(sem, time_offset):
        async with sem:
            chunk_buf = audio_file[
                  time_offset * TEN_MINUTES * 1_000: time_offset * TEN_MINUTES * 1_000 + TEN_MINUTES * 1_000]
            chunk_export = functools.partial(chunk_buf.export, f"app/static/downloads/tmp_{time_offset}.mp3", format="mp3")
            await asyncio.to_thread(chunk_export)

            app.logger.info(
                f"Processing tmp_{time_offset}.mp3 start={time_offset * TEN_MINUTES},"
                f" end={time_offset * TEN_MINUTES + TEN_MINUTES}")
            rsp = await async_transcript(f"app/static/downloads/tmp_{time_offset}.mp3", dnl_format, language)
            heapq.heappush(chunks_heap, (time_offset, rsp))
            pathlib.Path(f"app/static/downloads/tmp_{time_offset}.mp3").unlink()

    if 'audio_file' not in request.files:
        return jsonify({'error': 'No audio file provided'})

    media_file = request.files['audio_file']
    app.logger.info(f"File Info: {media_file.filename=}, {media_file.content_type=}")

    if not media_file.filename.endswith(('.mp3', '.mp4', '.wav', '.ogg')):
        return jsonify({'error': 'Invalid audio file format'})

    dnl_format = request.form.get('format', 'text')
    language = request.form.get('language', 'en')

    path_to_tmp_mp3_file = f"app/static/downloads/{media_file.filename}"
    async with aiofiles.open(path_to_tmp_mp3_file, 'w+b') as tmp_file:
        while buf := media_file.read(1024 * 1024):
            await tmp_file.write(buf)

    if media_file.filename.endswith('.mp4'):
        app.logger.info(f"Converting {media_file.filename} to MP3")
        video = VideoFileClip(f"app/static/downloads/{media_file.filename}")
        await async_write_audiofile(f"app/static/downloads/{TMP_MEDIA_FILE}", video.audio)
        app.logger.info(f"Media file {media_file.filename} was converted to {TMP_MEDIA_FILE}")
        path_to_tmp_mp3_file = f"app/static/downloads/{TMP_MEDIA_FILE}"

    if pathlib.Path(f"{path_to_tmp_mp3_file}").stat().st_size >= 25 * MiB:
        app.logger.info(f"Media file is greater than {25 * MiB}, splitting to chunks ...")
        audio_file = AudioSegment.from_file(f"{path_to_tmp_mp3_file}")
        len_in_sec = len(audio_file) // 1000

        app.logger.info(f"Audio file duration={timedelta(seconds=len_in_sec)} min")
        concurrency_limit = 8
        semaphore = asyncio.Semaphore(concurrency_limit)
        tasks = [_process_chunk(semaphore, offset) for offset in range(len_in_sec // TEN_MINUTES)]
        await asyncio.gather(*tasks)

        subtitles_buf = ''.join(heapq.heappop(chunks_heap)[1] for _ in range(len(chunks_heap)))
    else:
        response = await async_transcript(path_to_tmp_mp3_file, dnl_format, language)
        subtitles_buf = response

    ext = 'srt' if dnl_format == 'srt' else 'txt'
    filename = f"subtitles_{datetime.now().strftime('%y_%m_%d_%H%M%S')}.{ext}"
    buf = BytesIO(subtitles_buf.encode())
    pathlib.Path(path_to_tmp_mp3_file).unlink(missing_ok=True)
    pathlib.Path(f"app/static/downloads/{media_file.filename}").unlink(missing_ok=True)
    return send_file(buf, as_attachment=True, download_name=filename)

@app.route('/download')
def download_file():
    dnl_format = request.form.get('format', 'text')
    ext = 'srt' if dnl_format == 'srt' else 'txt'
    file_path = f"app/static/downloads/subtitles.{ext}"

    with open(file_path, "rb") as f:
        buf = BytesIO(f.read())
    return send_file(buf, as_attachment=True, download_name=f"subtitles.{ext}")

from app.mod_1 import views