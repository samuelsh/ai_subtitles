# Import flask and template operators
from io import BytesIO

from flask import Flask, render_template, jsonify, request, send_file
import openai

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


@app.route('/transcribe', methods=['POST'])
def transcribe():
    if 'audio_file' not in request.files:
        return jsonify({'error': 'No audio file provided'})

    audio_file = request.files['audio_file']
    print("File Info:", audio_file.filename, audio_file.content_type)

    # Ensure the file has a valid format (you may need to adjust this based on your requirements)
    if not audio_file.filename.endswith(('.mp3', 'mp4', '.wav', '.ogg')):
        return jsonify({'error': 'Invalid audio file format'})

<<<<<<< HEAD
    dnl_format = request.form.get('format', 'txt')
=======
    dnl_format = request.form.get('format', 'text')
>>>>>>> 696a89f (subtitles-creator: basic working prototype)

    # Call OpenAI API to generate subtitles
    file_buf = BytesIO(audio_file.read())
    file_buf.name = "tmp.mp3"
    try:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=file_buf,
            language='ru',
            response_format=dnl_format
        )
        with open("app/static/downloads/subtitles.srt", "wt") as f:
            f.write(str(response))
        return jsonify({'subtitles': response})
    except Exception as e:
        return jsonify({'error': f'Error processing audio: {str(e)}'})


@app.route('/download')
def download_file():
    # Replace 'path/to/your/file.txt' with the actual path to the file you want to serve
    file_path = 'app/static/downloads/subtitles.srt'

    # You can also specify a custom filename for the downloaded file
    with open(file_path, "rb") as f:
        buf = BytesIO(f.read())
    return send_file(buf, as_attachment=True, download_name="subtitles.txt")


# Import modules here
from app.mod_1 import views

# Build the database:
# This will init the db
# db.init_app(app)
