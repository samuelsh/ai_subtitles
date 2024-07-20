# Use the official Python image as the base image
FROM python:3.9-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set the working directory in the container
WORKDIR /app

# Copy the dependencies file to the working directory
COPY requirements.txt .

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code to the working directory
COPY . /app

# Expose port 5000 to the outside world
EXPOSE 5000

# Command to run the application
CMD ["gunicorn", "--access-logfile", "log/access.log", "--error-logfile", "log/error.log", "--log-level", "debug", "--timeout", "3600", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]