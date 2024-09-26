import pandas as pd
import numpy as np
from sklearn.cluster import DBSCAN
from tqdm import tqdm


def cluster_roads(trips_relevant_segments, epsilon=10, min_samples=10, first_cl_idx=0):
    """
    trips_relevant_segments: a subset of segments, each segment goes through the same nodes (one or pair)
    first_cl_idx: what nr to start enumerating clusters with 
    Clusters the segments:
    - Trims the segments around the node to facilitate clustering
    - Applies DBSCAN
    - adds a column to the subset
    """
    # Trim segments to those that are not close to a node

    def _trim_segment(group, trim_start=3, trim_end=3):

        total_trim = trim_start + trim_end

        if len(group) > total_trim + 1:
            return group.iloc[trim_start:-trim_end]
        else:
            return group

    subset_clustering = trips_relevant_segments.loc[trips_relevant_segments["cl_idx"].isna()]
    subset_clustering = subset_clustering.groupby(
        'SegmentId').apply(_trim_segment).reset_index(drop=True)
    if not subset_clustering.empty:
        # Example parameters
        dbscan = DBSCAN(eps=epsilon, min_samples=min_samples)
        clusters = dbscan.fit_predict(subset_clustering.loc[:, ["x", "y"]])
        subset_clustering["cluster"] = np.array([c + first_cl_idx if c != -1 else -1 for c in clusters]).astype('int64')
    else:
        subset_clustering["cluster"] = np.nan

    return subset_clustering


def cluster_all_connected_roads(segments_df, node_segment_summary, epsilon=10, min_samples=10):
    """
    Clusters roads that pass through the same nodes. 
    Returns a dataframe with the cluster assignment ('cluster') and the most dominant clusters for each segment ('mode_cluster')
    segments_df: df with annotated trips divided into segments (as returned by divide_trips_at_intersections()) 
    node_segment_summary: df summarizing the connections.
                          each row is node IDs and their corresponding SegmentIds 
                          (as returned by summarize_segments_and_nodes())
    """
    segments_cluster_list = []
    first_cl_idx = 0
    max_cluster = 0

    for i, row in node_segment_summary.iterrows():
        subset = segments_df.loc[segments_df["SegmentId"].isin(
            row["segment_id_list"])]
        subset = cluster_roads(
            subset, epsilon=epsilon, min_samples=min_samples, first_cl_idx=first_cl_idx)
        if not subset["cluster"].empty and np.max(subset["cluster"]) > max_cluster:
            max_cluster = int(np.max(subset["cluster"]))
        # max_cluster = int(np.max(subset["cluster"])) if not subset["cluster"].empty else -1
        subset = subset.sort_values(["SegmentId", "timestamp_s"])
        segments_cluster_list.append(subset)
        first_cl_idx = max_cluster + 1  # if max_cluster >= 0 else 0

    segments_cluster_df = pd.concat(segments_cluster_list, axis=0)

    # Find which cluster is the most dominant in each segment.
    mode_series = segments_cluster_df.groupby("SegmentId")["cluster"].agg(
        lambda x: pd.Series.mode(x)[0])
    segments_cluster_df['mode_cluster'] = segments_cluster_df['SegmentId'].map(
        mode_series)

    return segments_cluster_df


def mark_trips_closest_to_intersection(trips_annotated):
    """
    Mark points where the trip passes the closest to an intersection.
    trips_annotated (as returned by find_relations()).
    """
    def _mark_minimal_distances(group):
        # Only proceed if there's at least one non-NaN 'dist' value in the group
        if group['dist'].notna().any():
            # Find the index of the minimal non-NaN 'dist' value
            min_dist_idx = group['dist'].idxmin()
            # Safely mark this index as having the minimal 'dist' value
            group.loc[min_dist_idx, 'is_min'] = True
        return group

    # Marks new group when 'dist' changes from NaN to not NaN
    trips_annotated['group'] = (trips_annotated['dist'].notna(
    ) & trips_annotated['dist'].shift().isna()).cumsum()
    trips_annotated['is_min'] = False

    # Apply the function to each group, ensuring groups with valid 'dist' values are processed
    trips_annotated = trips_annotated.groupby(['TripLogId', 'group']).apply(
        _mark_minimal_distances).reset_index(drop=True)
    return trips_annotated


def divide_trips_at_intersections(trips_annotated):
    """
    Divides the trips data into smaller segments. Each trip is cut when it passes next to an intersection.
    The point where it passes is repeated, so that the cl_idx can be retrieved for each SegmentId.
    trips_annotated is sorted by trip and time.
    Adds a new "SegmentId" column.
    """
    segments = []

    for trip_id, trip_data in tqdm(trips_annotated.groupby('TripLogId'), desc="Dividing trips into segments:"):
        start_idx = 0
        counter = 0

        if any(trip_data["is_min"]):
            # For each trip, start from zero. Then iterate through rows. If there is no "is_min", then it's just one segment.
            # If there is, do the first trip up until that point.
            # Each time you find a min, append a segment and take a step back with the index.

            # Iterate through the trip data
            for i, (idx, row) in enumerate(trip_data.iterrows()):

                if (not row['is_min']) or (i == 0):
                    continue

                # If 'is_min' is True and there's a start index, slice the trip segment
                if row['is_min'] and (start_idx != 0):
                    segment = trip_data.loc[start_idx:idx]
                    segments.append(segment)
                    counter += 1

                # Update the start index for the next segment
                start_idx = idx-1

            # Handle the last segment from the last 'is_min' to the end of the trip
            if (start_idx != 0):
                segment = trip_data.loc[start_idx:]
                segments.append(segment)
        else:
            segments.append(trip_data)

    # Concatenate all segments into a single DataFrame with a new 'SegmentId' column
    segments_df = pd.concat(segments, keys=range(len(segments))).reset_index(
        level=0).rename(columns={'level_0': 'SegmentId'}).reset_index(drop=True)

    return segments_df



def summarize_segments_and_nodes(segments_df):
    """
    Finds all segments that pass through one or two intersections.
    Summarizes them into a dataframe with intersection indices and their corresponding SegmentIds.
    """
    # Return pairs of nodes that are connected by some segments. If a segment passes through only one node, only that node is mentioned.
    # For each segment, show what nodes does this segment cut through
    segments_nodes = segments_df.loc[segments_df["is_min"]].groupby("SegmentId")[
        "cl_idx"].unique()

    # Show unique node pairs (if a segment cuts through both) / nodes (if a segment cuts through only one)
    node_sets = sorted(list(set([tuple(sorted(s)) for s in segments_nodes])))

    segment_ids_list = []
    for s in node_sets:

        if len(s) == 2:
            segments_0 = segments_df.loc[(segments_df["cl_idx"] == s[0]) & (
                segments_df["is_min"]), "SegmentId"].unique()
            segments_1 = segments_df.loc[(segments_df["cl_idx"] == s[1]) & (
                segments_df["is_min"]), "SegmentId"].unique()
            segments = [s for s in segments_0 if (s in segments_1)]

        elif len(s) == 1:
            segments = segments_df.loc[(segments_df["cl_idx"] == s[0]) & (
                segments_df["is_min"]), "SegmentId"].unique()

        segment_ids_list.append(segments)

    node_segment_summary = pd.DataFrame(
        [(s,) for s in node_sets], columns=["nodes"])
    node_segment_summary["segment_id_list"] = segment_ids_list

    return node_segment_summary


def filter_node_summary_duplicates(node_segment_summary):
    """
    Filter out SegmentIds in singular nodes if they already occur in pairs.
    """
    df = node_segment_summary
    segment_ids_in_pairs = set()
    for index, row in df.iterrows():
        if len(row['nodes']) == 2:  # Check if the node is a pair
            segment_ids_in_pairs.update(row['segment_id_list'])

    # Step 2: Filter segment IDs for single nodes
    for index, row in df.iterrows():
        if len(row['nodes']) == 1:  # Check if the node is a single node
            # Keep only segment IDs not found in any pairs
            filtered_segments = [
                seg_id for seg_id in row['segment_id_list'] if seg_id not in segment_ids_in_pairs]
            df.at[index, 'segment_id_list'] = filtered_segments

    return df


def generate_edges(segments_cluster_df, segments_df, min_segment_length=0):
    """
    segments_cluster_df: clustered segments, without parts around the intersections
    segments_df: segments with all points
    """

    def _get_median_index(d):
        ranks = d.rank(pct=True)
        close_to_median = abs(ranks - 0.5)
        return close_to_median.idxmin()

    def _produce_edge(segments_subset, segments_df, min_segment_length):
        segment_length = segments_subset.groupby(
            "SegmentId").count()["Latitude"]
        segment_length = segment_length.loc[segment_length > min_segment_length]

        if not segment_length.empty:
            median_idx = _get_median_index(d=segment_length)
            chosen_edge = segments_df[segments_df["SegmentId"] == median_idx]
            chosen_edge = chosen_edge.sort_values("timestamp_s")
            return chosen_edge
        else:
            return None

    edges = []
    
    unique_clusters = segments_cluster_df["mode_cluster"].unique()
    for i, c in enumerate(tqdm(unique_clusters, desc="Generating edges from clusters: ")):
        if c != -1:
            segments_subset = segments_cluster_df.loc[segments_cluster_df["mode_cluster"] == c]
            chosen_edge = _produce_edge(segments_subset, segments_df, min_segment_length=min_segment_length)
            
            if chosen_edge is not None:
                node_id1 = chosen_edge["cl_idx"].iloc[0]
                node_id2 = chosen_edge["cl_idx"].iloc[-1]
                
                # Handling NaN node ids
                node_id1 = -1 if np.isnan(node_id1) else int(node_id1)
                node_id2 = -1 if np.isnan(node_id2) else int(node_id2)

                edge_dict = dict({
                    "id": f"e_{i}",
                    "coordinates": np.array(chosen_edge.loc[:, ["Longitude", "Latitude", "Altitude"]]),
                    "description": "road between two nodes",
                    "Node1": {"nodeId": node_id1},
                    "Node2": {"nodeId": node_id2}
                })

                edges.append(edge_dict)

    print(f"Graph edges were generated. Number of edges: {len(edges)}")

    return edges

