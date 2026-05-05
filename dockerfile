FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY . .
CMD ["python", "main.py"]
