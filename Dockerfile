FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sunset_cruise_bot.py .

CMD ["python", "sunset_cruise_bot.py"]
