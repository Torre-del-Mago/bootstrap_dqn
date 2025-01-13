# Use the official PyTorch image with CUDA support
FROM pytorch/pytorch:1.10.0-cuda11.3-cudnn8-runtime

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install the dependencies from requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Command to run when the container starts
CMD ["python", "run_bootstrap.py", "-c", "1"]