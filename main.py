import argparse
import random
import time
from concurrent import futures
import heapq
import math
from collections import defaultdict, deque
from typing import List, Tuple, Dict, Set, Optional

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
        self.enemy_positions = defaultdict(list)  # Track enemy movement history
        self.enemy_patterns = {}  # Predicted enemy movement patterns
        self.current_path = []  # Current planned path
        self.path_target = None  # Current pathfinding target
        self.energy_reserve = 20  # Minimum energy to keep in reserve
        self.turn_history = []  # Track game state history
        self.threat_zones = set()  # Areas under enemy threat
        self.pathfinding_cache = {}  # Cache for pathfinding results
        self.enemy_lighthouse_control = {}  # Track enemy lighthouse control timing
        self.strategic_zones = set()  # High-value strategic positions
        self.connection_threats = []  # Enemy connection attempts to block

    def calculate_triangle_area(self, p1, p2, p3):
        x1, y1 = p1
        x2, y2 = p2
        x3, y3 = p3
        return abs((x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2)) / 2.0)

    def heuristic(self, a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def get_neighbors(self, pos):
        x, y = pos
        neighbors = []
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < self.map_width and 0 <= ny < self.map_height:
                    neighbors.append((nx, ny))
        return neighbors

    def a_star_pathfind(self, start, goal, obstacles=None):
        if obstacles is None:
            obstacles = set()
            
        cache_key = (start, goal, tuple(sorted(obstacles)))
        if cache_key in self.pathfinding_cache:
            return self.pathfinding_cache[cache_key]
            
        if start == goal:
            return [start]
            
        open_set = [(0, start)]
        came_from = {}
        g_score = {start: 0}
        f_score = {start: self.heuristic(start, goal)}
        
        while open_set:
            current = heapq.heappop(open_set)[1]
            
            if current == goal:
                path = []
                while current in came_from:
                    path.append(current)
                    current = came_from[current]
                path.append(start)
                path.reverse()
                self.pathfinding_cache[cache_key] = path
                return path
                
            for neighbor in self.get_neighbors(current):
                if neighbor in obstacles:
                    continue
                    
                tentative_g_score = g_score[current] + 1
                
                if neighbor not in g_score or tentative_g_score < g_score[neighbor]:
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g_score
                    f_score[neighbor] = g_score[neighbor] + self.heuristic(neighbor, goal)
                    heapq.heappush(open_set, (f_score[neighbor], neighbor))
                    
        return None  # No path found

    def predict_enemy_movement(self, turn):
        current_turn = len(self.turn_history)
        
        # Track enemy positions
        for player_id in range(1, 5):  # Assuming max 4 players
            if player_id == self.player_num:
                continue
                
            # Find enemy position (simplified - in real game you'd track this better)
            enemy_lighthouses = [lh for lh in turn.Lighthouses if lh.Owner == player_id]
            
            if enemy_lighthouses:
                # Predict enemy will try to connect their lighthouses
                for i, lh1 in enumerate(enemy_lighthouses):
                    for lh2 in enemy_lighthouses[i+1:]:
                        # Mark areas between enemy lighthouses as threat zones
                        x1, y1 = lh1.Position.X, lh1.Position.Y
                        x2, y2 = lh2.Position.X, lh2.Position.Y
                        
                        # Add limited points along the line as threats (less conservative)
                        steps = max(abs(x2-x1), abs(y2-y1))
                        if steps > 0 and steps <= 6:  # Only mark short connections as threats
                            for step in range(1, steps):  # Skip endpoints
                                threat_x = x1 + (x2-x1) * step // steps
                                threat_y = y1 + (y2-y1) * step // steps
                                self.threat_zones.add((threat_x, threat_y))

    def calculate_zone_control_value(self, pos, lighthouses):
        x, y = pos
        value = 0
        
        # Corner positions are extremely valuable
        if pos in self.corner_positions:
            value += 200
        # Edge positions are valuable
        elif x == 0 or x == 14 or y == 0 or y == 14:
            value += 50
        # Center positions provide good connectivity
        elif 6 <= x <= 8 and 6 <= y <= 8:
            value += 30
            
        # Small bonus for being near our lighthouses (reduced defensive bias)
        owned_lighthouses = [lh for lh in lighthouses if lh.Owner == self.player_num]
        for lh in owned_lighthouses:
            distance = abs(x - lh.Position.X) + abs(y - lh.Position.Y)
            if distance <= 2:
                value += 8 - distance * 3
                
        # Reduced penalty for being in threat zones
        if pos in self.threat_zones:
            value -= 15
            
        return value

    def optimize_energy_allocation(self, turn, target_energy_needed):
        current_energy = turn.Energy
        
        # Calculate dynamic energy reserve based on threats
        base_reserve = 20
        threat_multiplier = len(self.threat_zones) // 20
        enemy_nearby = any(lh.Owner != self.player_num and lh.Owner != 0 
                          for lh in turn.Lighthouses 
                          if abs(lh.Position.X - turn.Position.X) + abs(lh.Position.Y - turn.Position.Y) <= 2)
        
        if enemy_nearby:
            dynamic_reserve = base_reserve + 15
        else:
            dynamic_reserve = base_reserve + threat_multiplier * 5
            
        # Calculate available energy for actions
        available_energy = max(0, current_energy - dynamic_reserve)
        
        return min(available_energy, target_energy_needed)

    def find_blocking_positions(self, turn):
        blocking_positions = []
        
        # Look for enemy lighthouse pairs that could form valuable connections
        enemy_lighthouses = [lh for lh in turn.Lighthouses if lh.Owner != self.player_num and lh.Owner != 0]
        
        for i, lh1 in enumerate(enemy_lighthouses):
            for lh2 in enemy_lighthouses[i+1:]:
                if lh1.Owner == lh2.Owner:  # Same enemy player
                    x1, y1 = lh1.Position.X, lh1.Position.Y
                    x2, y2 = lh2.Position.X, lh2.Position.Y
                    
                    # Calculate midpoint as blocking position
                    mid_x = (x1 + x2) // 2
                    mid_y = (y1 + y2) // 2
                    
                    # Check if blocking position is strategically valuable
                    if (mid_x, mid_y) in self.corner_positions:
                        blocking_positions.append(((mid_x, mid_y), 100))
                    elif mid_x == 0 or mid_x == 14 or mid_y == 0 or mid_y == 14:
                        blocking_positions.append(((mid_x, mid_y), 50))
                    else:
                        blocking_positions.append(((mid_x, mid_y), 25))
                        
        return sorted(blocking_positions, key=lambda x: x[1], reverse=True)

    def find_best_connection(self, current_pos, owned_lighthouses):
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
        
        # Update turn history for analysis
        self.turn_history.append(turn)
        
        # Predict enemy movements and update threat assessment
        self.predict_enemy_movement(turn)
        
        lighthouses = dict()
        for lh in turn.Lighthouses:
            pos = (lh.Position.X, lh.Position.Y)
            lighthouses[pos] = lh
            # Memorize all lighthouse positions we've seen
            self.known_lighthouse_positions.add(pos)
            
            # Track enemy lighthouse control timing
            if lh.Owner != self.player_num and lh.Owner != 0:
                if pos not in self.enemy_lighthouse_control:
                    self.enemy_lighthouse_control[pos] = len(self.turn_history)
                    
        # Update strategic zones based on current game state
        self.update_strategic_zones(lighthouses)

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
                    
                    # Enhanced connection strategy with blocking consideration
                    best_connection = self.find_best_connection((cx, cy), owned_lighthouses)
                    
                    # Prioritize connections that also block enemy strategies
                    blocking_positions = self.find_blocking_positions(turn)
                    blocking_targets = [pos for pos, _ in blocking_positions]
                    
                    strategic_connections = [conn for conn in possible_connections if conn in blocking_targets]
                    
                    if strategic_connections and best_connection in strategic_connections:
                        possible_connection = best_connection
                    elif strategic_connections:
                        possible_connection = strategic_connections[0]
                    elif best_connection and best_connection in possible_connections:
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
                
                # Use optimized energy allocation
                optimal_energy = self.optimize_energy_allocation(turn, min_energy + lighthouse_energy)
                
                if optimal_energy >= min_energy:
                    energy = min(optimal_energy, turn.Energy - self.energy_reserve)
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

        # Enhanced lighthouse targeting with multiple strategies
        best_lighthouse = None
        best_score = float('-inf')
        
        # Check for blocking opportunities first
        blocking_positions = self.find_blocking_positions(turn)
        
        for lh in turn.Lighthouses:
            lh_x, lh_y = lh.Position.X, lh.Position.Y
            lh_pos = (lh_x, lh_y)
            
            # Use A* pathfinding for accurate distance calculation
            obstacles = self.threat_zones.copy()
            path = self.a_star_pathfind((cx, cy), lh_pos, obstacles)
            
            if path is None:
                continue  # No valid path
                
            distance = len(path) - 1
            score = 0
            
            # Reduced distance penalty to encourage exploration
            score -= distance * 0.8
            
            # Zone control value
            zone_value = self.calculate_zone_control_value(lh_pos, turn.Lighthouses)
            score += zone_value
            
            # Blocking bonus
            blocking_bonus = next((value for pos, value in blocking_positions if pos == lh_pos), 0)
            score += blocking_bonus
            
            # Ownership considerations with exploration bonus
            if lh.Owner == 0:  # Unowned
                score += 85
                # Bonus for exploring distant unowned lighthouses
                if distance > 5:
                    score += 25
            elif lh.Owner != self.player_num:  # Enemy owned
                score += 60
                # Extra bonus for disrupting enemy triangles
                if lh_pos in [pos for pos, _ in blocking_positions[:3]]:
                    score += 40
            else:  # Already ours
                score += 15
            
            # Energy efficiency
            if lh.Owner != self.player_num:
                available_energy = self.optimize_energy_allocation(turn, lh.Energy + 1)
                if available_energy > lh.Energy:
                    score += (available_energy - lh.Energy) // 8
                else:
                    score -= (lh.Energy - available_energy) // 3
            
            if score > best_score:
                best_score = score
                best_lighthouse = lh_pos
        
        if best_lighthouse:
            # Use A* pathfinding for movement
            obstacles = self.threat_zones.copy()
            path = self.a_star_pathfind((cx, cy), best_lighthouse, obstacles)
            
            if path and len(path) > 1:
                next_pos = path[1]
                dx = next_pos[0] - cx
                dy = next_pos[1] - cy
                move = (dx, dy)
            else:
                # Fallback to direct movement
                target_x, target_y = best_lighthouse
                dx = 0 if target_x == cx else (1 if target_x > cx else -1)
                dy = 0 if target_y == cy else (1 if target_y > cy else -1)
                move = (dx, dy)
        else:
            # Strategic positioning when no clear target
            strategic_moves = []
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    test_pos = (cx + dx, cy + dy)
                    if (0 <= test_pos[0] < self.map_width and 0 <= test_pos[1] < self.map_height and
                        test_pos not in self.threat_zones):
                        zone_value = self.calculate_zone_control_value(test_pos, turn.Lighthouses)
                        strategic_moves.append(((dx, dy), zone_value))
            
            if strategic_moves:
                strategic_moves.sort(key=lambda x: x[1], reverse=True)
                move = strategic_moves[0][0]
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
        
    def update_strategic_zones(self, lighthouses):
        self.strategic_zones.clear()
        
        # Identify key strategic positions
        owned_lighthouses = [(lh.Position.X, lh.Position.Y) for lh in lighthouses.values() if lh.Owner == self.player_num]
        
        # Add positions that could form large triangles with our lighthouses
        for i, pos1 in enumerate(owned_lighthouses):
            for pos2 in owned_lighthouses[i+1:]:
                # Find third point that would create maximum triangle area
                for pos3 in self.corner_positions:
                    if pos3 not in owned_lighthouses:
                        area = self.calculate_triangle_area(pos1, pos2, pos3)
                        if area > 50:  # Significant area threshold
                            self.strategic_zones.add(pos3)
                            
        # Add defensive positions around our lighthouses
        for pos in owned_lighthouses:
            x, y = pos
            for dx in [-2, -1, 0, 1, 2]:
                for dy in [-2, -1, 0, 1, 2]:
                    if dx == 0 and dy == 0:
                        continue
                    def_pos = (x + dx, y + dy)
                    if (0 <= def_pos[0] < self.map_width and 0 <= def_pos[1] < self.map_height):
                        self.strategic_zones.add(def_pos)


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
