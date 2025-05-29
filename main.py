import argparse
import random
import time
from concurrent import futures
from collections import defaultdict

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
        self.enemy_lighthouses = defaultdict(set)  # player_id -> set of (x,y) positions
        self.turn_number = 0  # Track game progression
        self.enemy_connections = defaultdict(list)  # Track enemy connections for disruption
        
        # HARDCODED LIGHTHOUSE POSITIONS - THE CHEEKY ADVANTAGE!
        self.fixed_lighthouses = {
            (9, 0), (2, 1), (12, 1), (6, 2), (3, 3), 
            (0, 4), (9, 4), (14, 4), (4, 5), (12, 5),
            (6, 7), (10, 7), (13, 7), (1, 8), (3, 9), (11, 9),
            (5, 11), (10, 11), (13, 11), (3, 13), (13, 13), (8, 14)
        }
        
        # Pre-calculated optimal triangle formations (sorted by area)
        self.mega_triangles = [
            # Corner mega-triangles (area ~98-112)
            [(0, 4), (14, 4), (8, 14)],   # Top corners to bottom
            [(0, 4), (13, 13), (14, 4)],  # Wide top triangle
            
            # Edge-based large triangles (area ~60-80)
            [(9, 0), (3, 13), (13, 13)],  # Top to bottom edges
            [(2, 1), (12, 1), (8, 14)],   # Near-top to bottom
            
            # Strategic mid-size triangles (area ~40-60)
            [(0, 4), (9, 4), (5, 11)],    # Left side triangle
            [(9, 4), (14, 4), (10, 11)],  # Right side triangle
        ]
        
        # Optimal paths from each corner spawn
        self.corner_rush_targets = {
            (0, 0): [(2, 1), (0, 4), (3, 3)],      # Top-left spawn
            (14, 0): [(12, 1), (14, 4), (13, 7)],  # Top-right spawn
            (0, 14): [(3, 13), (1, 8), (0, 4)],    # Bottom-left spawn
            (14, 14): [(13, 13), (13, 7), (14, 4)] # Bottom-right spawn
        }

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
    
    def would_complete_enemy_triangle(self, pos, enemy_id):
        """Check if capturing this lighthouse would break an enemy triangle opportunity"""
        enemy_lhs = self.enemy_lighthouses.get(enemy_id, set())
        if len(enemy_lhs) < 2:
            return False
        
        # Check if this position would form a triangle with any two enemy lighthouses
        enemy_list = list(enemy_lhs)
        for i in range(len(enemy_list)):
            for j in range(i + 1, len(enemy_list)):
                area = self.calculate_triangle_area(pos, enemy_list[i], enemy_list[j])
                if area > 50:  # Significant triangle
                    return True
        return False
    
    def predict_capture_turns(self, lighthouse, our_energy):
        """Predict how many turns until we could capture this lighthouse"""
        if lighthouse.Owner == 0:  # Unowned
            return 0 if our_energy > lighthouse.Energy else 999
        
        # Owned lighthouses lose 10 energy per turn
        energy_decay_rate = 10
        current_energy = lighthouse.Energy
        turns = 0
        
        while current_energy >= our_energy and turns < 10:
            current_energy -= energy_decay_rate
            turns += 1
        
        return turns if current_energy < our_energy else 999
    
    def get_cluster_density(self, pos, radius=3):
        """Count lighthouses within radius of position"""
        count = 0
        px, py = pos
        for lh_pos in self.known_lighthouse_positions:
            lx, ly = lh_pos
            if abs(px - lx) <= radius and abs(py - ly) <= radius:
                count += 1
        return count
    
    def line_intersects(self, p1, p2, p3, p4):
        """Check if line segment p1-p2 intersects with p3-p4"""
        def ccw(A, B, C):
            return (C[1]-A[1]) * (B[0]-A[0]) > (B[1]-A[1]) * (C[0]-A[0])
        
        # Check if segments intersect
        return ccw(p1, p3, p4) != ccw(p2, p3, p4) and ccw(p1, p2, p3) != ccw(p1, p2, p4)
    
    def would_connection_cross(self, from_pos, to_pos, existing_connections):
        """Check if a new connection would cross existing ones"""
        for conn_pair in existing_connections:
            if self.line_intersects(from_pos, to_pos, conn_pair[0], conn_pair[1]):
                return True
        return False

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
        self.turn_number += 1
        
        # Initialize spawn corner on first turn for optimal pathing
        if self.turn_number == 1:
            self.spawn_corner = None
            min_dist = 999
            for corner in self.corner_positions:
                dist = abs(cx - corner[0]) + abs(cy - corner[1])
                if dist < min_dist:
                    min_dist = dist
                    self.spawn_corner = corner
        
        # Track time on lighthouse
        current_pos = (cx, cy)
        if current_pos == self.last_lighthouse_pos:
            self.turns_on_lighthouse += 1
        else:
            self.turns_on_lighthouse = 0
            
        lighthouses = dict()
        # Clear enemy tracking for fresh update
        self.enemy_lighthouses.clear()
        
        for lh in turn.Lighthouses:
            pos = (lh.Position.X, lh.Position.Y)
            lighthouses[pos] = lh
            # Memorize all lighthouse positions we've seen
            self.known_lighthouse_positions.add(pos)
            
            # Track enemy lighthouses
            if lh.Owner != 0 and lh.Owner != self.player_num:
                self.enemy_lighthouses[lh.Owner].add(pos)
                # Track enemy connections too
                for conn in lh.Connections:
                    conn_pos = (conn.X, conn.Y)
                    self.enemy_connections[lh.Owner].append((pos, conn_pos))

        if (cx, cy) in lighthouses:
            self.last_lighthouse_pos = current_pos
            
            # Force movement if we've been camping too long (1 turn max for 12-player ultra-aggression)
            if self.turns_on_lighthouse >= 1:
                # Skip to movement logic below - ALWAYS BE MOVING!
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
                    
                    # Get all existing connections for validation
                    all_connections = []
                    for lh in owned_lighthouses:
                        lh_pos = (lh.Position.X, lh.Position.Y)
                        for conn in lh.Connections:
                            conn_pos = (conn.X, conn.Y)
                            all_connections.append((lh_pos, conn_pos))
                    
                    # Filter out connections that would cross existing ones
                    valid_connections = []
                    for dest in possible_connections:
                        if not self.would_connection_cross((cx, cy), dest, all_connections):
                            valid_connections.append(dest)
                    
                    if valid_connections:
                        best_connection = self.find_best_connection((cx, cy), owned_lighthouses)
                        
                        if best_connection and best_connection in valid_connections:
                            possible_connection = best_connection
                        else:
                            possible_connection = random.choice(valid_connections)
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
            elif self.turns_on_lighthouse < 1:  # Only attack on first turn at lighthouse
                lighthouse_energy = lighthouses[(cx, cy)].Energy
                if turn.Energy > lighthouse_energy:
                    min_energy = lighthouse_energy + 1
                energy_ratio = turn.Energy / max(min_energy, 1)
                
                # Calculate optimal investment based on strategic value
                is_corner = (cx, cy) in self.corner_positions
                is_edge = cx == 0 or cx == 14 or cy == 0 or cy == 14
                owned_count = len([lh for lh in turn.Lighthouses if lh.Owner == self.player_num])
                
                # Game phase-aware energy management
                if self.turn_number < 10:
                    # Early game: minimal investment, maximize expansion
                    energy = min_energy
                    max_spend_ratio = 0.3  # Keep 70% for movement
                elif self.turn_number < 30:
                    # Mid game: balanced investment
                    if energy_ratio >= 3.0:
                        energy = min(min_energy + 10, turn.Energy // 3)
                    else:
                        energy = min_energy
                    max_spend_ratio = 0.5  # Keep 50% for movement
                else:
                    # Late game: can invest more if strategic
                    if energy_ratio >= 2.0:
                        energy = min(min_energy + 20, turn.Energy // 2)
                    else:
                        energy = min_energy
                    max_spend_ratio = 0.7  # Can spend more late game
                
                # CHEEKY SPEEDRUN: Always use minimal energy to maximize expansion
                # With 12 players, quantity > quality
                if self.turn_number < 15:
                    energy = min_energy  # Absolute minimum early game
                else:
                    energy = min(min_energy + 5, turn.Energy * 0.3)  # Still very conservative
                
                # Hard cap based on game phase
                max_allowed = int(turn.Energy * max_spend_ratio)
                energy = min(energy, max_allowed)
                
                # Never go below movement reserve
                min_reserve = 40 if self.turn_number < 30 else 20
                if energy > turn.Energy - min_reserve:
                    energy = max(min_energy, turn.Energy - min_reserve)
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
        owned_positions = {(lh.Position.X, lh.Position.Y) for lh in owned_lighthouses}
        
        # CHEEKY STRATEGY: Use pre-planned routes based on spawn corner
        if self.turn_number <= 10 and hasattr(self, 'spawn_corner') and self.spawn_corner in self.corner_rush_targets:
            # Follow optimal early game path
            for target_pos in self.corner_rush_targets[self.spawn_corner]:
                if target_pos not in owned_positions and target_pos in self.fixed_lighthouses:
                    # Check if this lighthouse exists in current game state
                    for lh in turn.Lighthouses:
                        if (lh.Position.X, lh.Position.Y) == target_pos:
                            best_lighthouse = target_pos
                            break
                    if best_lighthouse:
                        break
        
        # If no pre-planned target or after early game, use smart scoring
        if not best_lighthouse:
            for lh in turn.Lighthouses:
                lh_x, lh_y = lh.Position.X, lh.Position.Y
                distance = abs(cx - lh_x) + abs(cy - lh_y)
                
                score = 0
                
                # Distance penalty (minimal for ultra-aggressive expansion)
                score -= distance * 0.5
                
                # CHEEKY BONUS: Prioritize lighthouses that form our pre-calculated mega triangles
                pos = (lh_x, lh_y)
                for triangle in self.mega_triangles:
                    if pos in triangle:
                        # Count how many of this triangle we already own
                        owned_in_triangle = sum(1 for t_pos in triangle if t_pos in owned_positions)
                        if owned_in_triangle == 2:
                            score += 500  # MASSIVE bonus to complete triangle
                        elif owned_in_triangle == 1:
                            score += 150  # Good bonus to continue triangle
                        else:
                            score += 50   # Small bonus to start triangle
            
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
            
            # Turn-aware strategy phases
            if self.turn_number < 10:
                # Early game: prioritize corners and edges
                strategy_multiplier = 1.5 if (lh_x, lh_y) in self.corner_positions else 1.0
            elif self.turn_number < 30:
                # Mid game: focus on triangle formation
                strategy_multiplier = 1.2
            else:
                # Late game: consolidate and disrupt
                strategy_multiplier = 0.8
            
            # Ownership status (12-player ultra-aggressive priorities)
            if lh.Owner == 0:  # Unowned - absolute top priority
                score += 150 * strategy_multiplier  # Massive bonus for new territory
            elif lh.Owner != self.player_num:  # Enemy owned - steal aggressively
                score += 100 * strategy_multiplier  # High priority to disrupt enemies
                
                # Check if this breaks enemy triangle opportunity
                for enemy_id in self.enemy_lighthouses:
                    if self.would_complete_enemy_triangle((lh_x, lh_y), enemy_id):
                        score += 200  # HUGE bonus for disruption
                        break
                
                # Ultra-aggressive stealing based on energy prediction
                capture_turns = self.predict_capture_turns(lh, turn.Energy)
                if capture_turns == 0:
                    score += 80  # Can capture now
                elif capture_turns <= 2:
                    score += 50  # Can capture soon
                elif capture_turns <= 4:
                    score += 20  # Worth positioning for
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
            
            # Cluster density bonus - control areas with many lighthouses
            cluster_density = self.get_cluster_density((lh_x, lh_y), radius=3)
            if cluster_density >= 4:
                score += 40  # Dense area bonus
            elif cluster_density >= 2:
                score += 20  # Moderate density bonus
            
                if score > best_score:
                    best_score = score
                    best_lighthouse = (lh_x, lh_y)
        
        # ULTRA CHEEKY: If we have 2+ lighthouses, always move toward completing triangles
        if len(owned_lighthouses) >= 2 and not best_lighthouse:
            # Find the nearest lighthouse that would complete a large triangle
            for triangle in self.mega_triangles:
                owned_in_triangle = [(pos, pos in owned_positions) for pos in triangle]
                if sum(owned for _, owned in owned_in_triangle) == 2:
                    # We own 2, find the missing one
                    for pos, owned in owned_in_triangle:
                        if not owned and pos in self.fixed_lighthouses:
                            dist = abs(cx - pos[0]) + abs(cy - pos[1])
                            if dist < 10:  # Reasonably close
                                best_lighthouse = pos
                                break
                    if best_lighthouse:
                        break
        
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
