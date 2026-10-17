import data from "../../../robot/floor4.json";

export const FLOOR_DATA = data;
export const CENTER_U = (data.geometry.junctionNear + data.geometry.junctionFar) / 2;
export const DOORS = data.doors.map((door) => {
  const placed = Boolean(door.branch && door.side);
  const distance = door.steps * data.stepMeters;
  const far = (door.branch === "left") === (door.side === "right");
  return {
    ...door, placed, distance,
    title: `${door.room}${door.entry ? ` · door ${door.entry}` : ""}`,
    v: placed ? distance * (door.branch === "left" ? -1 : 1) : null,
    wall: placed ? (far ? "far" : door.branch === "left" ? "nearLeft" : "nearRight") : null,
    u: placed ? data.geometry[far ? "junctionFar" : "junctionNear"] : null,
  };
});

export function doorFeatures() {
  return DOORS.filter((d) => d.placed).map((d) => ({
    wall: d.wall, a0: d.v - (d.width || 0.8) / 2, a1: d.v + (d.width || 0.8) / 2,
    kind: "door", label: d.title, doorId: d.id,
  }));
}
