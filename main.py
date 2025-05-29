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
        self.lighthouse_target_queue = []  # Queue of lighthouse positions to visit
        self.current_target_index = 0  # Current target in rotation

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
        current_turn = len(self.turn_history)
        
        # Dynamic corner and edge valuation based on game phase
        if pos in self.corner_positions:
            corner_value = 250  # Increased base value
            # Early game: corners are extremely important
            if current_turn < 25:
                corner_value += 100
            # Late game: still valuable but less critical
            elif current_turn > 60:
                corner_value -= 50
            value += corner_value
            
        # Edge positions are valuable, especially in early game
        elif x == 0 or x == 14 or y == 0 or y == 14:
            edge_value = 60
            if current_turn < 30:
                edge_value += 30  # Early game bonus for edge control
            value += edge_value
            
        # Center positions provide good connectivity
        elif 6 <= x <= 8 and 6 <= y <= 8:
            center_value = 35
            # Mid-game: center becomes more valuable for connections
            if 20 <= current_turn <= 50:
                center_value += 20
            value += center_value
            
        # Strategic ring positions (one step from edges)
        elif ((x == 1 or x == 13) and 1 <= y <= 13) or ((y == 1 or y == 13) and 1 <= x <= 13):
            value += 25  # Secondary strategic positions
            
        # Connectivity bonus for being near our lighthouses
        owned_lighthouses = [lh for lh in lighthouses if lh.Owner == self.player_num]
        
        # Calculate potential triangle areas with owned lighthouses
        triangle_potential = 0
        if len(owned_lighthouses) >= 1:
            for i, lh1 in enumerate(owned_lighthouses):
                pos1 = (lh1.Position.X, lh1.Position.Y)
                for lh2 in owned_lighthouses[i+1:]:
                    pos2 = (lh2.Position.X, lh2.Position.Y)
                    area = self.calculate_triangle_area(pos, pos1, pos2)
                    triangle_potential = max(triangle_potential, area)
        
        value += triangle_potential * 1.5
        
        # Proximity bonus (reduced defensive bias but still useful)
        for lh in owned_lighthouses:
            distance = abs(x - lh.Position.X) + abs(y - lh.Position.Y)
            if distance <= 3:
                proximity_bonus = max(0, 12 - distance * 3)
                value += proximity_bonus
                
        # Dynamic threat zone penalty
        if pos in self.threat_zones:
            threat_penalty = 20
            # Early game: less concerned about threats, focus on expansion
            if current_turn < 15:
                threat_penalty = 5
            # Late game: more careful about threats
            elif current_turn > 50:
                threat_penalty = 35
            value -= threat_penalty
            
        return value

    def optimize_energy_allocation(self, turn, target_energy_needed):
        current_energy = turn.Energy
        current_turn = len(self.turn_history)
        
        # Calculate dynamic energy reserve based on threats and game phase
        base_reserve = 15  # Reduced base reserve for more aggressive play
        
        # Early game: be more aggressive with energy
        if current_turn < 20:
            base_reserve = 10
        # Mid game: standard reserve
        elif current_turn < 50:
            base_reserve = 15
        # Late game: more conservative to protect positions
        else:
            base_reserve = 25
            
        threat_multiplier = len(self.threat_zones) // 20
        enemy_nearby = any(lh.Owner != self.player_num and lh.Owner != 0 
                          for lh in turn.Lighthouses 
                          if abs(lh.Position.X - turn.Position.X) + abs(lh.Position.Y - turn.Position.Y) <= 2)
        
        # More nuanced energy management
        owned_lighthouses = len([lh for lh in turn.Lighthouses if lh.Owner == self.player_num])
        
        if enemy_nearby:
            dynamic_reserve = base_reserve + 10
        elif owned_lighthouses >= 3:  # We have good position, be more aggressive
            dynamic_reserve = max(base_reserve - 5, 5)
        else:
            dynamic_reserve = base_reserve + threat_multiplier * 3
            
        # Calculate available energy for actions with efficiency bonus
        available_energy = max(0, current_energy - dynamic_reserve)
        
        # Energy efficiency bonus for high-value targets
        if target_energy_needed > current_energy * 0.7:
            available_energy = min(current_energy - 5, available_energy + 10)
        
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

    def update_lighthouse_rotation_queue(self, lighthouses):
        """Update the lighthouse rotation queue with known lighthouse positions"""
        all_lighthouse_positions = list(self.known_lighthouse_positions)
        
        if not self.lighthouse_target_queue:
            # Initialize queue with all known lighthouses, prioritizing corners first
            corner_lighthouses = [pos for pos in all_lighthouse_positions if pos in self.corner_positions]
            edge_lighthouses = [pos for pos in all_lighthouse_positions 
                              if pos not in self.corner_positions and 
                              (pos[0] == 0 or pos[0] == 14 or pos[1] == 0 or pos[1] == 14)]
            other_lighthouses = [pos for pos in all_lighthouse_positions 
                               if pos not in corner_lighthouses and pos not in edge_lighthouses]
            
            # Prioritize: corners -> edges -> others
            self.lighthouse_target_queue = corner_lighthouses + edge_lighthouses + other_lighthouses
        else:
            # Add any new lighthouse positions we've discovered
            for pos in all_lighthouse_positions:
                if pos not in self.lighthouse_target_queue:
                    self.lighthouse_target_queue.append(pos)
    
    def get_next_lighthouse_target(self, current_pos, lighthouses):
        """Get the next lighthouse target in rotation that makes strategic sense"""
        if not self.lighthouse_target_queue:
            return None
            
        # Filter out lighthouses we already control and don't need to revisit
        owned_lighthouses = {(lh.Position.X, lh.Position.Y) for lh in lighthouses.values() 
                           if lh.Owner == self.player_num}
        
        # Look for the next uncontrolled lighthouse in our queue
        attempts = 0
        while attempts < len(self.lighthouse_target_queue):
            if self.current_target_index >= len(self.lighthouse_target_queue):
                self.current_target_index = 0  # Wrap around
                
            target_pos = self.lighthouse_target_queue[self.current_target_index]
            
            # Skip if we already own this lighthouse and it doesn't need defense
            if target_pos in owned_lighthouses:
                # Check if this lighthouse needs defense
                needs_defense = False
                tx, ty = target_pos
                for lh in lighthouses.values():
                    if lh.Owner != self.player_num and lh.Owner != 0:
                        enemy_distance = abs(lh.Position.X - tx) + abs(lh.Position.Y - ty)
                        if enemy_distance <= 4:  # Enemy nearby
                            needs_defense = True
                            break
                
                if not needs_defense:
                    self.current_target_index += 1
                    attempts += 1
                    continue
            
            # This is a good target
            return target_pos
            
        # If we've checked all lighthouses and they're all owned, start over
        self.current_target_index = 0
        return self.lighthouse_target_queue[0] if self.lighthouse_target_queue else None

    def find_best_connection(self, current_pos, owned_lighthouses):
        cx, cy = current_pos
        
        if len(owned_lighthouses) < 2:
            return None
            
        best_target = None
        max_score = 0
        current_turn = len(self.turn_history)
        
        for target in owned_lighthouses:
            target_pos = (target.Position.X, target.Position.Y)
            if target_pos == (cx, cy):
                continue
                
            for third in owned_lighthouses:
                third_pos = (third.Position.X, third.Position.Y)
                if third_pos == (cx, cy) or third_pos == target_pos:
                    continue
                    
                area = self.calculate_triangle_area((cx, cy), target_pos, third_pos)
                score = area
                
                # Enhanced corner bonuses based on game phase
                corner_bonus = 0
                if target_pos in self.corner_positions and third_pos in self.corner_positions:
                    corner_bonus = 150  # Massive bonus for corner-to-corner triangles
                elif target_pos in self.corner_positions or third_pos in self.corner_positions:
                    corner_bonus = 60   # Good bonus for one corner
                    
                # Early game: prioritize corner control
                if current_turn < 30:
                    corner_bonus *= 1.5
                    
                score += corner_bonus
                
                # Bonus for edge positions (creates defensive barriers)
                edge_count = 0
                for pos in [target_pos, third_pos]:
                    x, y = pos
                    if x == 0 or x == 14 or y == 0 or y == 14:
                        edge_count += 1
                score += edge_count * 20
                
                # Strategic positioning bonus
                # Prefer triangles that span multiple quadrants
                quadrants = set()
                for pos in [(cx, cy), target_pos, third_pos]:
                    qx, qy = pos
                    quadrant = (qx >= 7, qy >= 7)
                    quadrants.add(quadrant)
                    
                if len(quadrants) >= 2:
                    score += 40  # Multi-quadrant triangle bonus
                    
                # Perimeter bonus - triangles with larger perimeter control more territory
                perimeter = (abs(cx - target_pos[0]) + abs(cy - target_pos[1]) +
                           abs(target_pos[0] - third_pos[0]) + abs(target_pos[1] - third_pos[1]) +
                           abs(third_pos[0] - cx) + abs(third_pos[1] - cy))
                score += perimeter * 2
                
                if score > max_score:
                    max_score = score
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
        
        # Update lighthouse rotation queue with newly discovered lighthouses
        self.update_lighthouse_rotation_queue(lighthouses)

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
                
                # If no connections possible and we own this lighthouse, consider moving away
                # to find new targets instead of staying put
                current_turn = len(self.turn_history)
                owned_lighthouses = [lh for lh in turn.Lighthouses if lh.Owner == self.player_num]
                
                # Only stay if this lighthouse needs defense or we have few lighthouses
                should_defend = False
                if len(owned_lighthouses) <= 2:  # Keep defending if we have few lighthouses
                    should_defend = True
                else:
                    # Check if enemies are nearby threatening this lighthouse
                    for lh in turn.Lighthouses:
                        if lh.Owner != self.player_num and lh.Owner != 0:
                            enemy_distance = abs(lh.Position.X - cx) + abs(lh.Position.Y - cy)
                            if enemy_distance <= 3:  # Enemy is close
                                should_defend = True
                                break
                
                # If we don't need to defend, move away to find new targets
                if not should_defend:
                    # Skip to movement logic below
                    pass
                else:
                    # Stay and defend - continue with attack/pass logic
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
            else:
                # Not our lighthouse, try to attack it
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
                    # Not enough energy to attack, move away to find easier targets
                    pass

        # Systematic lighthouse targeting using rotation queue
        current_turn = len(self.turn_history)
        owned_lighthouses = [lh for lh in turn.Lighthouses if lh.Owner == self.player_num]
        
        # Get next lighthouse target from rotation system
        rotation_target = self.get_next_lighthouse_target((cx, cy), lighthouses)
        
        # If we have a rotation target, prefer it, but still check for urgent threats
        best_lighthouse = rotation_target
        blocking_positions = self.find_blocking_positions(turn)
        
        # Override rotation target if there are urgent strategic opportunities
        urgent_targets = []
        
        # Check for high-priority targets that should override rotation
        for lh in turn.Lighthouses:
            lh_x, lh_y = lh.Position.X, lh.Position.Y
            lh_pos = (lh_x, lh_y)
            
            # Skip if this is too far and we have a closer rotation target
            distance_to_target = abs(cx - lh_x) + abs(cy - lh_y)
            if rotation_target:
                distance_to_rotation = abs(cx - rotation_target[0]) + abs(cy - rotation_target[1])
                if distance_to_target > distance_to_rotation * 2:
                    continue  # Too far, stick with rotation
            
            urgency_score = 0
            
            # Urgent: unowned corner lighthouses in early game
            if (lh.Owner == 0 and lh_pos in self.corner_positions and 
                current_turn < 25):
                urgency_score += 200
                
            # Urgent: enemy about to complete a large triangle
            if lh.Owner != self.player_num and lh.Owner != 0:
                blocking_bonus = next((value for pos, value in blocking_positions if pos == lh_pos), 0)
                if blocking_bonus >= 50:  # High blocking value
                    urgency_score += 150
                    
            # Urgent: easy target with good energy efficiency
            if lh.Owner != self.player_num:
                required_energy = lh.Energy + 1 if lh.Owner != 0 else 1
                available_energy = self.optimize_energy_allocation(turn, required_energy)
                if available_energy >= required_energy and distance_to_target <= 3:
                    urgency_score += 100
            
            if urgency_score >= 150:  # High urgency threshold
                urgent_targets.append((lh_pos, urgency_score, distance_to_target))
        
        # Choose urgent target if any exist
        if urgent_targets:
            # Sort by urgency score, then by distance
            urgent_targets.sort(key=lambda x: (-x[1], x[2]))
            best_lighthouse = urgent_targets[0][0]
            # Update rotation to next lighthouse since we're deviating
            self.current_target_index = (self.current_target_index + 1) % max(1, len(self.lighthouse_target_queue))
        
        if best_lighthouse:
            # Use A* pathfinding for movement toward lighthouse target
            obstacles = self.threat_zones.copy()
            path = self.a_star_pathfind((cx, cy), best_lighthouse, obstacles)
            
            if path and len(path) > 1:
                next_pos = path[1]
                dx = next_pos[0] - cx
                dy = next_pos[1] - cy
                move = (dx, dy)
                
                # If we're moving toward our rotation target, advance the index
                if best_lighthouse == rotation_target:
                    target_distance = abs(cx - best_lighthouse[0]) + abs(cy - best_lighthouse[1])
                    if target_distance <= 2:  # Close to target, prepare next target
                        self.current_target_index = (self.current_target_index + 1) % max(1, len(self.lighthouse_target_queue))
            else:
                # Fallback to direct movement
                target_x, target_y = best_lighthouse
                dx = 0 if target_x == cx else (1 if target_x > cx else -1)
                dy = 0 if target_y == cy else (1 if target_y > cy else -1)
                move = (dx, dy)
        else:
            # Enhanced strategic positioning when no clear target
            strategic_moves = []
            current_turn = len(self.turn_history)
            
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    if dx == 0 and dy == 0:
                        continue
                    test_pos = (cx + dx, cy + dy)
                    if (0 <= test_pos[0] < self.map_width and 0 <= test_pos[1] < self.map_height):
                        zone_value = self.calculate_zone_control_value(test_pos, turn.Lighthouses)
                        
                        # Early game: prioritize corner/edge exploration
                        if current_turn < 20:
                            if test_pos in self.corner_positions:
                                zone_value += 150
                            elif test_pos[0] == 0 or test_pos[0] == 14 or test_pos[1] == 0 or test_pos[1] == 14:
                                zone_value += 75
                        
                        # Avoid threat zones but don't completely exclude them
                        if test_pos in self.threat_zones:
                            zone_value -= 30
                        
                        # Exploration bonus for unseen areas
                        if test_pos not in [pos for pos in self.known_lighthouse_positions]:
                            zone_value += 20
                        
                        strategic_moves.append(((dx, dy), zone_value))
            
            if strategic_moves:
                strategic_moves.sort(key=lambda x: x[1], reverse=True)
                # Add some randomness to avoid predictable patterns
                if len(strategic_moves) > 1 and current_turn > 10:
                    # 70% chance to pick best, 30% chance to pick second best
                    if random.random() < 0.7:
                        move = strategic_moves[0][0]
                    else:
                        move = strategic_moves[1][0]
                else:
                    move = strategic_moves[0][0]
            else:
                # Fallback movement with bias toward edges in early game
                if current_turn < 15:
                    # Prefer moves toward edges/corners in early game
                    edge_moves = []
                    for dx in [-1, 0, 1]:
                        for dy in [-1, 0, 1]:
                            if dx == 0 and dy == 0:
                                continue
                            new_x, new_y = cx + dx, cy + dy
                            if (0 <= new_x < self.map_width and 0 <= new_y < self.map_height):
                                # Prioritize moves toward edges
                                edge_distance = min(new_x, new_y, 14 - new_x, 14 - new_y)
                                if edge_distance <= 2:
                                    edge_moves.append((dx, dy))
                    
                    if edge_moves:
                        move = random.choice(edge_moves)
                    else:
                        moves = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
                        move = random.choice(moves)
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
