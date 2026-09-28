using Godot;

public partial class Player : Node2D
{
    [Export] public float Speed { get; set; } = 60.0f;
    [Export] public int Hp = 3;
    private int _ticks;

    public override void _Ready()
    {
        GD.Print("ready hp=", Hp);
    }

    public override void _Process(double delta)
    {
        Position = new Vector2(Position.X + Speed * (float)delta, Position.Y);
        _ticks = _ticks + 1;
        if (_ticks == 60)
        {
            GD.Print("x=", Position.X);
        }
    }
}
