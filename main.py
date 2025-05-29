import argparse
import random
import time
from concurrent import futures
from collections import deque

import grpc
from google.protobuf import json_format
from grpc import RpcError

from internal.handler.coms import game_pb2
from internal.handler.coms import game_pb2_grpc as game_grpc

timeout_to_response = 1  # 1 second


class BotGameTurn:
    def __init__(self, turn, action):
        self.turn = turn
        self.action = action


class BotGame:
    def __init__(self, player_num=None):
        self.player_num = player_num
        self.initial_state = None
        self.turn_states = []
        self.countT = 1
        self.map_size = 15
        self.known_lighthouses = set()
        self.target_lighthouse = None
        self.owned_lighthouses = set()
        self.visited = set()
        
    def parse_initial_map(self):
        if not self.initial_state:
            return
        for lh in self.initial_state.Lighthouses:
            self.known_lighthouses.add((lh.Position.X, lh.Position.Y))
    
    def manhattan_distance(self, p1, p2):
        return abs(p1[0] - p2[0]) + abs(p1[1] - p2[1])
    
    def find_closest_unowned_lighthouse(self, pos, lighthouses):
        closest = None
        min_dist = float('inf')
        for lh_pos, lh in lighthouses.items():
            if lh.Owner != self.player_num:
                dist = self.manhattan_distance(pos, lh_pos)
                if dist < min_dist:
                    min_dist = dist
                    closest = lh_pos
        return closest
    
    def get_best_move_towards(self, current, target):
        cx, cy = current
        tx, ty = target
        
        moves = []
        for dx, dy in ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < self.map_size and 0 <= ny < self.map_size:
                new_dist = self.manhattan_distance((nx, ny), target)
                moves.append((new_dist, (dx, dy)))
        
        moves.sort()
        return moves[0][1] if moves else (0, 0)
    
    def should_connect(self, current_lh, lighthouses):
        best_connection = None
        max_benefit = 0
        
        for dest_pos, dest_lh in lighthouses.items():
            if (dest_pos == current_lh.Position.X, current_lh.Position.Y):
                continue
            if not dest_lh.HaveKey:
                continue
            if dest_lh.Owner != self.player_num:
                continue
            
            already_connected = False
            for conn in dest_lh.Connections:
                if conn.X == current_lh.Position.X and conn.Y == current_lh.Position.Y:
                    already_connected = True
                    break
            if already_connected:
                continue
                
            dist = self.manhattan_distance(
                (current_lh.Position.X, current_lh.Position.Y),
                (dest_pos[0], dest_pos[1])
            )
            benefit = 100 - dist
            
            if benefit > max_benefit:
                max_benefit = benefit
                best_connection = dest_pos
                
        return best_connection

    def new_turn_action(self, turn: game_pb2.NewTurn) -> game_pb2.NewAction:
        if self.countT == 1 and self.initial_state:
            self.parse_initial_map()
            
        cx, cy = turn.Position.X, turn.Position.Y
        current_pos = (cx, cy)

        lighthouses = dict()
        for lh in turn.Lighthouses:
            lighthouses[(lh.Position.X, lh.Position.Y)] = lh

        if current_pos in lighthouses:
            current_lh = lighthouses[current_pos]
            
            if current_lh.Owner == self.player_num:
                self.owned_lighthouses.add(current_pos)
                
                connection_target = self.should_connect(current_lh, lighthouses)
                if connection_target:
                    action = game_pb2.NewAction(
                        Action=game_pb2.CONNECT,
                        Destination=game_pb2.Position(
                            X=connection_target[0], Y=connection_target[1]
                        ),
                    )
                    bgt = BotGameTurn(turn, action)
                    self.turn_states.append(bgt)
                    self.countT += 1
                    return action
                    
                closest_unowned = self.find_closest_unowned_lighthouse(current_pos, lighthouses)
                if closest_unowned:
                    self.target_lighthouse = closest_unowned
            else:
                attack_energy = min(turn.Energy, max(1, current_lh.Energy + 1))
                action = game_pb2.NewAction(
                    Action=game_pb2.ATTACK,
                    Energy=attack_energy,
                    Destination=game_pb2.Position(X=cx, Y=cy),
                )
                bgt = BotGameTurn(turn, action)
                self.turn_states.append(bgt)
                self.countT += 1
                return action

        if not self.target_lighthouse or self.target_lighthouse in self.owned_lighthouses:
            self.target_lighthouse = self.find_closest_unowned_lighthouse(current_pos, lighthouses)
        
        if self.target_lighthouse:
            move = self.get_best_move_towards(current_pos, self.target_lighthouse)
        else:
            unvisited_lh = [lh for lh in self.known_lighthouses if lh not in self.visited]
            if unvisited_lh:
                target = min(unvisited_lh, key=lambda lh: self.manhattan_distance(current_pos, lh))
                move = self.get_best_move_towards(current_pos, target)
            else:
                moves = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
                move = random.choice(moves)
        
        self.visited.add(current_pos)
        
        action = game_pb2.NewAction(
            Action=game_pb2.MOVE,
            Destination=game_pb2.Position(
                X=cx + move[0], Y=cy + move[1]
            ),
        )

        bgt = BotGameTurn(turn, action)
        self.turn_states.append(bgt)

        self.countT += 1
        return action


class BotComs:
    def __init__(self, bot_name, my_address, game_server_address, verbose=False):
        self.bot_id = None
        self.bot_name = bot_name
        self.my_address = my_address
        self.game_server_address = game_server_address
        self.verbose = verbose

    def wait_to_join_game(self):
        channel = grpc.insecure_channel(self.game_server_address)
        client = game_grpc.GameServiceStub(channel)

        player = game_pb2.NewPlayer(name=self.bot_name, serverAddress=self.my_address)

        while True:
            try:
                player_id = client.Join(player, timeout=timeout_to_response)
                self.bot_id = player_id.PlayerID
                print(f"Joined game with ID {player_id.PlayerID}")
                if self.verbose:
                    print(json_format.MessageToJson(player_id))
                break
            except RpcError as e:
                print(f"Could not join game: {e.details()}")
                time.sleep(1)

    def start_listening(self):
        print("Starting to listen on", self.my_address)

        # configure gRPC server
        grpc_server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=10),
            interceptors=(ServerInterceptor(),),
        )

        # registry of the service
        cs = ClientServer(bot_id=self.bot_id, verbose=self.verbose)
        game_grpc.add_GameServiceServicer_to_server(cs, grpc_server)

        # server start
        grpc_server.add_insecure_port(self.my_address)
        grpc_server.start()

        try:
            grpc_server.wait_for_termination()  # wait until server finish
        except KeyboardInterrupt:
            grpc_server.stop(0)


class ServerInterceptor(grpc.ServerInterceptor):
    def intercept_service(self, continuation, handler_call_details):
        start_time = time.time_ns()
        method_name = handler_call_details.method

        # Invoke the actual RPC
        response = continuation(handler_call_details)

        # Log after the call
        duration = time.time_ns() - start_time
        print(f"Unary call: {method_name}, Duration: {duration:.2f} nanoseconds")
        return response


class ClientServer(game_grpc.GameServiceServicer):
    def __init__(self, bot_id, verbose=False):
        self.bg = BotGame(bot_id)
        self.verbose = verbose

    def Join(self, request, context):
        return None

    def InitialState(self, request, context):
        print("Receiving InitialState")
        if self.verbose:
            print(json_format.MessageToJson(request))
        self.bg.initial_state = request
        return game_pb2.PlayerReady(Ready=True)

    def Turn(self, request, context):
        print(f"Processing turn: {self.bg.countT}")
        if self.verbose:
            print(json_format.MessageToJson(request))
        action = self.bg.new_turn_action(request)
        return action


def ensure_params():
    parser = argparse.ArgumentParser(description="Bot configuration")
    parser.add_argument("--bn", type=str, default="random-bot", help="Bot name")
    parser.add_argument("--la", type=str, required=True, help="Listen address")
    parser.add_argument("--gs", type=str, required=True, help="Game server address")

    args = parser.parse_args()

    if not args.bn:
        raise ValueError("Bot name is required")
    if not args.la:
        raise ValueError("Listen address is required")
    if not args.gs:
        raise ValueError("Game server address is required")

    return args.bn, args.la, args.gs


def main():
    verbose = False
    bot_name, listen_address, game_server_address = ensure_params()

    bot = BotComs(
        bot_name=bot_name,
        my_address=listen_address,
        game_server_address=game_server_address,
        verbose=verbose,
    )
    bot.wait_to_join_game()
    bot.start_listening()


if __name__ == "__main__":
    main()
