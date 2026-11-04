FROM python:3.13-alpine
RUN apk add --no-cache openssl && adduser -D -u 10001 gitgate
COPY gitgate.py /app/gitgate.py
USER gitgate
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD ["python3", "-c", "import socket; socket.create_connection(('127.0.0.1', 8080), 2)"]
CMD ["python3", "-I", "/app/gitgate.py"]
