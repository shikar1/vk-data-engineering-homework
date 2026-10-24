FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir kazoo==2.10.0
COPY . /app
ENTRYPOINT ["python", "-u", "lab.py"]
