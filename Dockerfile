# Use the official Python image from the Docker Hub
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Copy the Python script and requirements file to the container
COPY . .

# Install the required Python packages
RUN pip install -r requirements.txt

# Run the script
CMD ["python", "script.py"]
