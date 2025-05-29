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
        self.turns_on_lighthouse = 0  # Track how long we've been on same lighthouse
        self.last_lighthouse_pos = None  # Track last lighthouse position

    def calculate_triangle_potential(self, new_pos, owned_lighthouses):
        """Calculate potential triangle area if we capture this lighthouse"""
        if len(owned_lighthouses) < 1:
            return 0
            
        max_potential = 0
        for lh1 in owned_lighthouses:
            pos1 = (lh1.Position.X, lh1.Position.Y)
            if len(owned_lighthouses) >= 2:
                for lh2 in owned_lighthouses:
                    pos2 = (lh2.Position.X, lh2.Position.Y)
                    if pos1 != pos2:
                        area = self.calculate_triangle_area(new_pos, pos1, pos2)
                        if area > max_potential:
                            max_potential = area
            else:
                # With only one owned lighthouse, estimate potential
                distance = abs(new_pos[0] - pos1[0]) + abs(new_pos[1] - pos1[1])
                max_potential = distance * 2  # Rough estimate
        
        return min(max_potential // 3, 50)  # Cap the bonus

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
        max_score = 0
        
        for target in owned_lighthouses:
            target_pos = (target.Position.X, target.Position.Y)
            if target_pos == (cx, cy):
                continue
                
            connection_score = 0
            max_triangle_area = 0
            
            for third in owned_lighthouses:
                third_pos = (third.Position.X, third.Position.Y)
                if third_pos == (cx, cy) or third_pos == target_pos:
                    continue
                    
                area = self.calculate_triangle_area((cx, cy), target_pos, third_pos)
                max_triangle_area = max(max_triangle_area, area)
                
            # Base score from triangle area
            connection_score = max_triangle_area
            
            # Strategic position bonuses
            if target_pos in self.corner_positions:
                if (cx, cy) in self.corner_positions:
                    connection_score += 200  # Corner to corner - massive bonus
                else:
                    connection_score += 80   # To corner - large bonus
            
            # Distance penalty (prefer closer connections first)
            distance = abs(cx - target_pos[0]) + abs(cy - target_pos[1])
            connection_score -= distance * 2
            
            # Bonus for creating new triangles vs extending existing ones
            creates_new_triangle = max_triangle_area > 0
            if creates_new_triangle:
                connection_score += 50
            
            if connection_score > max_score:
                max_score = connection_score
                best_target = target_pos
                    
        return best_target

    def new_turn_action(self, turn: game_pb2.NewTurn) -> game_pb2.NewAction:
        cx, cy = turn.Position.X, turn.Position.Y
        
        self.lastY = cy
        self.lastX = cx
        
        # Track time on lighthouse
        current_pos = (cx, cy)
        if current_pos == self.last_lighthouse_pos:
            self.turns_on_lighthouse += 1
        else:
            self.turns_on_lighthouse = 0
            
        lighthouses = dict()
        for lh in turn.Lighthouses:
            pos = (lh.Position.X, lh.Position.Y)
            lighthouses[pos] = lh
            # Memorize all lighthouse positions we've seen
            self.known_lighthouse_positions.add(pos)

        if (cx, cy) in lighthouses:
            self.last_lighthouse_pos = current_pos
            
            # Force movement if we've been camping too long (3 turns max)
            if self.turns_on_lighthouse >= 3:
                # Skip to movement logic below
                pass
            # Conectar con faro remoto válido si podemos
            elif lighthouses[(cx, cy)].Owner == self.player_num:
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

            # Skip attack if we've been camping too long
            elif self.turns_on_lighthouse < 3:
                lighthouse_energy = lighthouses[(cx, cy)].Energy
                if turn.Energy > lighthouse_energy:
                    min_energy = lighthouse_energy + 1
                energy_ratio = turn.Energy / max(min_energy, 1)
                
                # Calculate optimal investment based on strategic value
                is_corner = (cx, cy) in self.corner_positions
                is_edge = cx == 0 or cx == 14 or cy == 0 or cy == 14
                owned_count = len([lh for lh in turn.Lighthouses if lh.Owner == self.player_num])
                
                # Base energy calculation
                if energy_ratio >= 4.0:
                    energy = min(min_energy + (lighthouse_energy // 2), turn.Energy // 2)
                elif energy_ratio >= 2.5:
                    energy = min(min_energy + (lighthouse_energy // 3), turn.Energy // 3)
                elif energy_ratio >= 1.5:
                    energy = min(min_energy + (lighthouse_energy // 4), max(turn.Energy - 30, min_energy))
                else:
                    energy = min_energy
                
                # Strategic adjustments
                if is_corner and owned_count < 2:
                    # Invest more in first corners for triangle potential
                    energy = min(energy + 20, turn.Energy - 10)
                elif is_edge and owned_count < 4:
                    # Moderate extra investment in edges
                    energy = min(energy + 10, turn.Energy - 20)
                
                # More aggressive energy spending for expansion
                if owned_count < 3 and energy > turn.Energy * 0.6:
                    energy = int(turn.Energy * 0.5)  # Keep more energy for movement
                elif owned_count < 6 and energy > turn.Energy * 0.7:
                    energy = int(turn.Energy * 0.6)
                    action = game_pb2.NewAction(
                        Action=game_pb2.ATTACK,
                        Energy=energy,
                        Destination=game_pb2.Position(X=turn.Position.X, Y=turn.Position.Y),
                    )
                    bgt = BotGameTurn(turn, action)
                    self.turn_states.append(bgt)

                    self.countT += 1
                    return action
            # NEVER PASS in 12-player game - always be moving!
            # Force movement logic below

        best_lighthouse = None
        best_score = float('-inf')
        owned_lighthouses = [lh for lh in turn.Lighthouses if lh.Owner == self.player_num]
        
        for lh in turn.Lighthouses:
            lh_x, lh_y = lh.Position.X, lh.Position.Y
            distance = abs(cx - lh_x) + abs(cy - lh_y)
            
            score = 0
            
            # Distance penalty (reduced for more aggressive expansion)
            score -= distance * 1.0
            
            # Strategic position bonuses
            if (lh_x, lh_y) in self.corner_positions:
                # Corners are extremely valuable for triangle formation
                score += 150
                # Extra bonus if we already own another corner
                owned_corners = sum(1 for olh in owned_lighthouses 
                                  if (olh.Position.X, olh.Position.Y) in self.corner_positions)
                if owned_corners > 0:
                    score += 100
            elif lh_x == 0 or lh_x == 14 or lh_y == 0 or lh_y == 14:
                # Edge positions good for large triangles
                score += 40
            elif 6 <= lh_x <= 8 and 6 <= lh_y <= 8:
                # Center positions for connectivity
                score += 25
            
            # Ownership status (12-player ultra-aggressive priorities)
            if lh.Owner == 0:  # Unowned - absolute top priority
                score += 150  # Massive bonus for new territory
            elif lh.Owner != self.player_num:  # Enemy owned - steal aggressively
                score += 100  # High priority to disrupt enemies
                # Ultra-aggressive stealing (attack with ANY advantage)
                if turn.Energy > lh.Energy - 5:
                    score += 60  # Attack even at slight disadvantage
                elif turn.Energy > lh.Energy - 15:
                    score += 30
            else:  # Already ours - strong penalty to force constant expansion
                score -= 60  # Big negative to prevent camping
                # Small exception for strategic positions
                if (lh_x, lh_y) in self.corner_positions:
                    score += 30  # Corners still somewhat valuable
            
            # Energy efficiency calculation
            if lh.Owner != self.player_num:
                energy_diff = turn.Energy - lh.Energy
                if energy_diff > 0:
                    # Bonus for easy captures
                    score += min(energy_diff // 8, 30)
                else:
                    # Heavy penalty for impossible captures
                    score -= (abs(energy_diff) // 3)
            
            # Triangle potential bonus
            if len(owned_lighthouses) >= 1:
                triangle_bonus = self.calculate_triangle_potential((lh_x, lh_y), owned_lighthouses)
                score += triangle_bonus
            
            if score > best_score:
                best_score = score
                best_lighthouse = (lh_x, lh_y)
        
        if best_lighthouse:
            target_x, target_y = best_lighthouse
            dx = 0 if target_x == cx else (1 if target_x > cx else -1)
            dy = 0 if target_y == cy else (1 if target_y > cy else -1)
            move = (dx, dy)
        else:
            # If no target, prefer edge/corner exploration
            edge_moves = []
            center_moves = []
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    new_x, new_y = cx + dx, cy + dy
                    if 0 <= new_x < self.map_width and 0 <= new_y < self.map_height:
                        # Prefer moves toward edges/corners
                        if (new_x <= 2 or new_x >= 12 or new_y <= 2 or new_y >= 12):
                            edge_moves.append((dx, dy))
                        else:
                            center_moves.append((dx, dy))
            
            move = random.choice(edge_moves if edge_moves else center_moves) if (edge_moves or center_moves) else (0, 0)
        
        new_x = turn.Position.X + move[0]
        new_y = turn.Position.Y + move[1]
        
        if new_x == self.lastX and new_y == self.lastY:
            # Avoid backtracking - find best alternative move
            alternative_moves = []
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    test_x = turn.Position.X + dx
                    test_y = turn.Position.Y + dy
                    if (test_x != self.lastX or test_y != self.lastY) and \
                       0 <= test_x < self.map_width and 0 <= test_y < self.map_height:
                        # Score alternative moves
                        move_score = 0
                        # Prefer continuing in same general direction
                        if best_lighthouse:
                            target_x, target_y = best_lighthouse
                            if (test_x - cx) * (target_x - cx) >= 0:  # Same x direction
                                move_score += 1
                            if (test_y - cy) * (target_y - cy) >= 0:  # Same y direction
                                move_score += 1
                        alternative_moves.append(((dx, dy), move_score))
            
            if alternative_moves:
                # Choose best scoring alternative, or random if tied
                max_score = max(score for _, score in alternative_moves)
                best_alternatives = [move for move, score in alternative_moves if score == max_score]
                move = random.choice(best_alternatives)
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
                # Can't find valid move but NEVER PASS - try random direction
                for _ in range(10):  # Try 10 times to find ANY valid move
                    dx, dy = random.choice([-1, 0, 1]), random.choice([-1, 0, 1])
                    if dx == 0 and dy == 0:
                        continue
                    test_x, test_y = turn.Position.X + dx, turn.Position.Y + dy
                    if 0 <= test_x < self.map_width and 0 <= test_y < self.map_height:
                        new_x, new_y = test_x, test_y
                        break

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
