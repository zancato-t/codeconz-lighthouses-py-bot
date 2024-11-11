FROM python:3.10-slim
ARG BOT_NAME="intelygenz-codeconz-lighthouses-py-bot"

WORKDIR /app
COPY ./ ./
COPY ./randbot.py ./main.py

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 3001
CMD [ "python3", "./main.py", "--bn=${BOT_NAME}", "--la=${BOT_NAME}:3001", "--gs=game:50051"
