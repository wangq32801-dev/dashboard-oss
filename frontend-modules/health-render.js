// Batch 6: health view's first executable rendering boundary.
// The monolith still owns DOM painting as a compatibility fallback, while this
// module owns the stable workout selection/summary model used by that painter.

export function selectHealthRides(data = {}) {
  const rides = (data.workouts || []).filter((workout) => {
    if (!workout) return false;
    const name = String(workout.name || workout.type || workout.activityType || '').toLowerCase();
    return /骑|bike|bicycle|cycle|cycling/.test(name);
  });
  const quality = data.workout_quality || {};
  return {
    rides,
    count: rides.length,
    distanceKm: rides.reduce((sum, row) => sum + (Number(row.distance_km) || 0), 0),
    durationMin: rides.reduce((sum, row) => sum + (Number(row.duration_min) || 0), 0),
    gpsCount: rides.filter((row) => row.distance_source === 'gps').length,
    missingCount: rides.filter((row) => row.distance_source === 'missing').length,
    derivedSpeedCount: rides.filter((row) => row.speed_source === 'derived').length,
    energyUnverified: !!(quality.energy && quality.energy.unverified),
  };
}
