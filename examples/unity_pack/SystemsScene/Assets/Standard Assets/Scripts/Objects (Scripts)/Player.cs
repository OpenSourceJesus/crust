using System;
using Extensions;
using UnityEngine;
using UnityEngine.InputSystem;

public class Player : MonoBehaviour
{
    public Rigidbody2D rb;
	public float moveSpeed;
	public float jumpSpeed;
	public Transform graphicsTrs;
	[HideInInspector]
	public Vector2 multSize = new Vector2(1, 1);
	float xSize = 1;

	void Start ()
	{
		Debug.Log("Hello World!");
		print(GameObject.Find("Bouncer").GetComponent<Bouncer>().amp);
		Console.WriteLine("End of Player.Start()");
	}

	void Update ()
	{
		float move = 0;
		if (Keyboard.current.leftArrowKey.isPressed)
			move --;
		if (Keyboard.current.rightArrowKey.isPressed)
			move ++;
		rb.linearVelocity = rb.linearVelocity.SetX(move * moveSpeed);
		if (move != 0)
			xSize = Mathf.Sign(move);
		graphicsTrs.SetWorldScale (multSize.SetX(multSize.x * xSize).SetZ(1));
		print(move);
		print("" + Time.time);
		SpriteRenderer spriteRend = gameObject.AddComponent<SpriteRenderer>();
		print(spriteRend);
	}

	void OnCollisionEnter2D (Collision2D coll)
	{
		print(coll);
	}
}
