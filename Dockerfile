FROM python:3.10-slim
ARG BOT_NAME
ENV BOT_NAME=${BOT_NAME}

WORKDIR /app
COPY ./ ./

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 3001
CMD [ "./entrypoint.sh" ]
