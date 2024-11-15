SERVER_PORT  := 50051
BOT_PORT	 := 3001
BOT_NAME	 := python-bot1

# Run py bot
runbotpy:
	python3 main.py --bn $(BOT_NAME) --la=localhost:$(BOT_PORT) --gs=localhost:$(SERVER_PORT)

.PHONY: runbotpy
