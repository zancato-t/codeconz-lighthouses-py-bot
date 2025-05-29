import argparse
import random
import time
from concurrent import futures

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
        self.lastY = 0
        self.lastX = 0
        self.map_width = 15  # Fixed map size
        self.map_height = 15
        self.corner_positions = [(0, 0), (0, 14), (14, 0), (14, 14)]
        self.known_lighthouse_positions = set()  # Track all lighthouse positions

    def calculate_triangle_area(self, p1, p2, p3):
        """Calculate area of triangle using shoelace formula"""
        x1, y1 = p1
        x2, y2 = p2
        x3, y3 = p3
        return abs((x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2)) / 2.0)

    def find_best_connection(self, current_pos, owned_lighthouses):
        """Find the connection that creates the largest triangle area"""
        cx, cy = current_pos
        
        if len(owned_lighthouses) < 2:
            return None
            
        best_target = None
        max_area = 0
        
        for target in owned_lighthouses:
            target_pos = (target.Position.X, target.Position.Y)
            if target_pos == (cx, cy):
                continue
                
            for third in owned_lighthouses:
                third_pos = (third.Position.X, third.Position.Y)
                if third_pos == (cx, cy) or third_pos == target_pos:
                    continue
                    
                area = self.calculate_triangle_area((cx, cy), target_pos, third_pos)
                
                # Bonus for corner-to-corner connections (massive triangles)
                if target_pos in self.corner_positions and third_pos in self.corner_positions:
                    area += 100  # Huge bonus for corner triangles
                elif target_pos in self.corner_positions or third_pos in self.corner_positions:
                    area += 25   # Smaller bonus for one corner
                
                if area > max_area:
                    max_area = area
                    best_target = target_pos
                    
        return best_target

    def new_turn_action(self, turn: game_pb2.NewTurn) -> game_pb2.NewAction:
        cx, cy = turn.Position.X, turn.Position.Y
        
        self.lastY = cy
        self.lastX = cx
        

        lighthouses = dict()
        for lh in turn.Lighthouses:
            pos = (lh.Position.X, lh.Position.Y)
            lighthouses[pos] = lh
            # Memorize all lighthouse positions we've seen
            self.known_lighthouse_positions.add(pos)

        if (cx, cy) in lighthouses:
            # Conectar con faro remoto válido si podemos
            if lighthouses[(cx, cy)].Owner == self.player_num:
                possible_connections = []
                for dest in lighthouses:
                    # No conectar con sigo mismo
                    # No conectar si no tenemos la clave
                    # No conectar si ya existe la conexión
                    # No conectar si no controlamos el destino
                    # Nota: no comprobamos si la conexión se cruza.
                    if (
                        dest != (cx, cy)
                        and lighthouses[dest].HaveKey
                        and not any(conn.X == cx and conn.Y == cy for conn in lighthouses[dest].Connections)
                        and lighthouses[dest].Owner == self.player_num
                    ):
                        possible_connections.append(dest)

                if possible_connections:
                    owned_lighthouses = [lh for lh in turn.Lighthouses if lh.Owner == self.player_num]
                    
                    best_connection = self.find_best_connection((cx, cy), owned_lighthouses)
                    
                    if best_connection and best_connection in possible_connections:
                        possible_connection = best_connection
                    else:
                        possible_connection = random.choice(possible_connections)
                    action = game_pb2.NewAction(
                        Action=game_pb2.CONNECT,
                        Destination=game_pb2.Position(
                            X=possible_connection[0], Y=possible_connection[1]
                        ),
                    )
                    bgt = BotGameTurn(turn, action)
                    self.turn_states.append(bgt)

                    self.countT += 1
                    return action

            lighthouse_energy = lighthouses[(cx, cy)].Energy
            if turn.Energy > lighthouse_energy:
                min_energy = lighthouse_energy + 1
                energy_ratio = turn.Energy / max(min_energy, 1)
                
                if energy_ratio >= 3.0:
                    energy = min(min_energy + (lighthouse_energy // 2), turn.Energy // 2)
                elif energy_ratio >= 2.0:
                    # Ensure we don't go negative and keep minimum reserve
                    energy = min(min_energy + (lighthouse_energy // 4), max(turn.Energy - 50, min_energy))
                else:
                    energy = min_energy
                action = game_pb2.NewAction(
                    Action=game_pb2.ATTACK,
                    Energy=energy,
                    Destination=game_pb2.Position(X=turn.Position.X, Y=turn.Position.Y),
                )
                bgt = BotGameTurn(turn, action)
                self.turn_states.append(bgt)

                self.countT += 1
                return action
            else:
                action = game_pb2.NewAction(
                    Action=game_pb2.PASS,
                    Destination=game_pb2.Position(X=turn.Position.X, Y=turn.Position.Y),
                )
                bgt = BotGameTurn(turn, action)
                self.turn_states.append(bgt)

                self.countT += 1
                return action

        best_lighthouse = None
        best_score = float('-inf')
        for lh in turn.Lighthouses:
            lh_x, lh_y = lh.Position.X, lh.Position.Y
            distance = abs(cx - lh_x) + abs(cy - lh_y)
            
            score = 0
            
            score -= distance * 2
            
            if (lh_x, lh_y) in self.corner_positions:
                score += 100
            
            elif lh_x == 0 or lh_x == 14 or lh_y == 0 or lh_y == 14:
                score += 30
                
            elif 6 <= lh_x <= 8 and 6 <= lh_y <= 8:
                score += 20
            
            if lh.Owner == 0:  # Unowned
                score += 50
            elif lh.Owner != self.player_num:  # Enemy owned
                score += 30
            else:  # Already ours
                score += 10
            
            if lh.Owner != self.player_num:
                if turn.Energy > lh.Energy:
                    score += (turn.Energy - lh.Energy) // 10
                else:
                    score -= (lh.Energy - turn.Energy) // 5
            
            if score > best_score:
                best_score = score
                best_lighthouse = (lh_x, lh_y)
        
        if best_lighthouse:
            target_x, target_y = best_lighthouse
            dx = 0 if target_x == cx else (1 if target_x > cx else -1)
            dy = 0 if target_y == cy else (1 if target_y > cy else -1)
            move = (dx, dy)
        else:
            moves = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
            move = random.choice(moves)
        
        new_x = turn.Position.X + move[0]
        new_y = turn.Position.Y + move[1]
        
        if new_x == self.lastX and new_y == self.lastY:
            alternative_moves = []
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    test_x = turn.Position.X + dx
                    test_y = turn.Position.Y + dy
                    if test_x != self.lastX or test_y != self.lastY:
                        alternative_moves.append((dx, dy))
            
            if alternative_moves:
                move = random.choice(alternative_moves)
                new_x = turn.Position.X + move[0]
                new_y = turn.Position.Y + move[1]
        
        # Use fixed map dimensions for boundary checking (always 15x15)
        if new_x < 0 or new_x >= self.map_width or new_y < 0 or new_y >= self.map_height:
            valid_moves = []
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    test_x = turn.Position.X + dx
                    test_y = turn.Position.Y + dy
                    if (0 <= test_x < self.map_width and 0 <= test_y < self.map_height and 
                        (test_x != self.lastX or test_y != self.lastY)):
                        valid_moves.append((dx, dy))
            
            if valid_moves:
                move = random.choice(valid_moves)
                new_x = turn.Position.X + move[0]
                new_y = turn.Position.Y + move[1]
            else:
                action = game_pb2.NewAction(
                    Action=game_pb2.PASS,
                    Destination=game_pb2.Position(X=turn.Position.X, Y=turn.Position.Y),
                )
                bgt = BotGameTurn(turn, action)
                self.turn_states.append(bgt)
                self.countT += 1
                return action

        action = game_pb2.NewAction(
            Action=game_pb2.MOVE,
            Destination=game_pb2.Position(X=new_x, Y=new_y),
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
