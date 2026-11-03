FROM python:3.13-alpine
RUN adduser -D -u 10001 gitgate
COPY gitgate.py /app/gitgate.py
USER gitgate
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/ 2>&1 | grep -q "not a git endpoint" || exit 1
CMD ["python3", "-I", "/app/gitgate.py"]
