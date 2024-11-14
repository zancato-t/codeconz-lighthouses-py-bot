# codeconz-lighthouses-py-bot

This is a Python bot that can be used to play the game of Lighthouses on the CodeConz platform.

## Interaction flow with the Game Engine
![Interaction Flow](./docs/interaction_flow.png)

The bot interacts with the game engine in three steps:
1. **Join Game**: The bot sends a join request to the game engine to join the game. The game engine responds with the Bot ID.
2. **Get Initial State**: The game engine sends get the initial state of the game to the bots.
3. **Turn request**: The game engine requests the bot to make an action on each turn, and sends the current state to the bot. The bot responds with the action.

For more in depth information on the game, please refer to the [Game Engine documentation](https://github.com/intelygenz/codeconz-lighthouses-engine/blob/master/README.md).