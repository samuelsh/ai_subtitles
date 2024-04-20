# Import flask and template operators
import pathlib
import asyncio
import aiohttp
import openai

from easypy.units import HOUR, MINUTE, GiB, MiB
from io import BytesIO
from datetime import datetime, timedelta
from pydub import AudioSegment
from moviepy.editor import VideoFileClip
from flask import Flask, render_template, jsonify, request, send_file, Response

TEN_MINUTES = 10 * 60
TMP_MEDIA_FILE = "tmp_media_file.mp3"
# Could import flask extensions, such as SQLAlchemy, here
# from flask.ext.sqlalchemy import SQLAlchemy

# Define WSGI object
app = Flask(__name__)

# Configurations
app.config.from_object('config')
# openai.api_key = app.config['OPENAI_API_KEY']
client = openai.OpenAI(api_key=app.config['OPENAI_API_KEY'])


# Some more example SQLAlchemy config
# Define the database object which is imported by modules and controllers
# db = SQLAlchemy(app)

# HTTP error handling
@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404


# Home page view
@app.route('/')
def home():
    return render_template('home.html')


async def async_transcript(path, dnl_format):
    with open(path, 'rb') as tmp_file:
        try:
            form_data = aiohttp.FormData()
            form_data.add_field("file", tmp_file, filename=pathlib.Path(path).name, content_type='multipart/form-data')
            for key, value in {
                        "model": "whisper-1",
                        "language": "ru",
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
                return await response.text()
        except aiohttp.ClientError as e:
            return jsonify({'error': f'Error processing audio: {e}'})


@app.route('/transcribe', methods=['POST'])
async def transcribe():
    subtitles_buf = ""

    if 'audio_file' not in request.files:
        return jsonify({'error': 'No audio file provided'})

    media_file = request.files['audio_file']
    app.logger.info(f"File Info: {media_file.filename=}, {media_file.content_type=}")

    # Ensure the file has a valid format (you may need to adjust this based on your requirements)
    if not media_file.filename.endswith(('.mp3', 'mp4', '.wav', '.ogg')):
        return jsonify({'error': 'Invalid audio file format'})

    dnl_format = request.form.get('format', 'text')

    path_to_tmp_mp3_file = f"app/static/downloads/{media_file.filename}"
    with open(path_to_tmp_mp3_file, 'w+b') as tmp_file:
        while buf := media_file.read(1024 * 1024):
            tmp_file.write(buf)

    if media_file.filename.endswith('.mp4'):
        app.logger.info(f"Converting {media_file.filename} to MP3")
        video = VideoFileClip(f"app/static/downloads/{media_file.filename}")
        video.audio.write_audiofile(f"app/static/downloads/{TMP_MEDIA_FILE}")
        app.logger.info(f"Media file {media_file.filename} was converted to {TMP_MEDIA_FILE}")
        path_to_tmp_mp3_file = f"app/static/downloads/{TMP_MEDIA_FILE}"

    if pathlib.Path(f"{path_to_tmp_mp3_file}").stat().st_size >= 25 * MiB:
        app.logger.info(f"Media file is greater then {25 * MiB}, splitting to chunks ...")
        audio_file = AudioSegment.from_file(f"{path_to_tmp_mp3_file}")
        len_in_sec = len(audio_file) // 1000

        app.logger.info(f"Audio file duration={timedelta(seconds=len_in_sec)} min")
        # Iterate over 10 minutes chunks
        for time_offset in range(len_in_sec // TEN_MINUTES):
            buf = audio_file[
                  time_offset * TEN_MINUTES * 1_000: time_offset * TEN_MINUTES * 1_000 + TEN_MINUTES * 1_000]
            buf.export(f"app/static/downloads/tmp_{time_offset}.mp3", format="mp3")

            app.logger.info(
                f"Processing tmp_{time_offset}.mp3 start={time_offset * TEN_MINUTES},"
                f" end={time_offset * TEN_MINUTES + TEN_MINUTES}")
            # with open(f"app/static/downloads/tmp_{time_offset}.mp3", 'rb') as tmp_file:
            #     try:
            #         response = client.audio.transcriptions.create(
            #             model="whisper-1",
            #             file=tmp_file,
            #             language='ru',
            #             response_format=dnl_format
            #         )
            #     except Exception as e:
            #         return jsonify({'error': f'Error processing audio: {str(e)}'})
            response = await async_transcript(f"app/static/downloads/tmp_{time_offset}.mp3", dnl_format)
            subtitles_buf += response
            pathlib.Path(f"app/static/downloads/tmp_{time_offset}.mp3").unlink()

    else:
        response = await async_transcript(path_to_tmp_mp3_file, dnl_format)
        subtitles_buf = response
        # Call OpenAI API to generate subtitles
        # with open(f"{path_to_tmp_mp3_file}", 'rb') as tmp_file:
        #     try:
        #         response = client.audio.transcriptions.create(
        #             model="whisper-1",
        #             file=tmp_file,
        #             language='ru',
        #             response_format=dnl_format
        #         )
        #
        #         subtitles_buf = response
        #
        #     except Exception as e:
        #         return jsonify({'error': f'Error processing audio: {str(e)}'})

    ext = 'srt' if dnl_format == 'srt' else 'txt'
    filename = f"subtitles_{datetime.now().strftime('%y_%m_%d_%H%M%S')}.{ext}"
    buf = BytesIO(subtitles_buf.encode())
    pathlib.Path(path_to_tmp_mp3_file).unlink(missing_ok=True)
    pathlib.Path(f"app/static/downloads/{media_file.filename}").unlink(missing_ok=True)
    return send_file(buf, as_attachment=True, download_name=filename)
    # return Response(response, mimetype='text/plain',
    #                 headers={'Content-Disposition': f'attachment;filename={filename}'})


@app.route('/download')
def download_file():
    # Replace 'path/to/your/file.txt' with the actual path to the file you want to serve
    dnl_format = request.form.get('format', 'text')
    ext = 'srt' if dnl_format == 'srt' else 'txt'
    file_path = f"app/static/downloads/subtitles.{ext}"

    # You can also specify a custom filename for the downloaded file
    with open(file_path, "rb") as f:
        buf = BytesIO(f.read())
    return send_file(buf, as_attachment=True, download_name=f"subtitles.{ext}")


# Import modules here
from app.mod_1 import views

# Build the database:
# This will init the db
# db.init_app(app)
