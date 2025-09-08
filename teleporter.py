import asyncio
from wizwalker.file_readers.wad import Wad
from wizwalker import XYZ
import trimesh
import numpy as np
from navmesh_parser import parse_nav_data

async def load_navmesh_for_zone(zone_name):
    """Load navmesh data from WAD file for the given zone."""
    adjusted_zone_name = zone_name.replace("/", "-")
    zone_wad = Wad.from_game_data(adjusted_zone_name)
    await zone_wad.open()
    
    navmesh_data = await zone_wad.get_file("zone.nav")
    vertices, edges = parse_nav_data(navmesh_data)
    
    zone_wad.close()
    return vertices, edges

def create_navmesh_path(vertices, edges):
    """Create a trimesh Path3D object from vertices and edges."""
    vertices_list = [[v.x, v.y, v.z] for v in vertices]
    path = trimesh.path.Path3D(
        entities=[
            trimesh.path.entities.Line(
                points=np.array([vertices_list[start], vertices_list[stop]])
            ) for start, stop in edges
        ]
    )
    return path

def find_closest_point_on_path(path, target_position):
    """Find the closest point on the navmesh path to the target position."""
    query_point = np.array([target_position.x, target_position.y, target_position.z])
    
    min_dist_sq = float('inf')
    closest_point_on_path = None

    for line in path.entities:
        p1 = line.points[0]
        p2 = line.points[1]

        line_vec = p2 - p1
        point_vec = query_point - p1

        line_len_sq = np.sum(line_vec**2)
        if line_len_sq == 0:
            continue
        
        t = np.dot(point_vec, line_vec) / line_len_sq

        if t < 0.0:
            closest_point_on_segment = p1
        elif t > 1.0:
            closest_point_on_segment = p2
        else:
            closest_point_on_segment = p1 + t * line_vec
        
        dist_sq = np.sum((query_point - closest_point_on_segment)**2)

        if dist_sq < min_dist_sq:
            min_dist_sq = dist_sq
            closest_point_on_path = closest_point_on_segment
    
    return closest_point_on_path

async def teleport_to_quest_position(client):
    """Teleport to quest position using navmesh pathfinding."""
    try:
        zone_name = await client.zone_name()
        print(f"Loading navmesh for zone: {zone_name}")

        # Load navmesh data
        vertices, edges = await load_navmesh_for_zone(zone_name)
        
        # Create path object
        path = create_navmesh_path(vertices, edges)
        print(f"Successfully loaded navmesh as Path3D")

        # Get target position
        target_position = await client.quest_position.position()
        if target_position.x == 0 and target_position.y == 0 and target_position.z == 0:
            await asyncio.sleep(0.5)
            return

        # Find closest point on navmesh
        closest_point_on_path = find_closest_point_on_path(path, target_position)
        
        if closest_point_on_path is not None:
            print(f"Teleporting to closest navmesh point: {closest_point_on_path}")
            await client.teleport(XYZ(closest_point_on_path[0], closest_point_on_path[1], closest_point_on_path[2]))
    except Exception as e:
        print(f"Error loading navmesh: {e}")