using Extensions;
using UnityEngine;
using UnityEngine.InputSystem;

public class Player : MonoBehaviour
{
    public Rigidbody2D rb;
	public float moveSpeed;
	public Transform graphicsTrs;
	[HideInInspector]
	public Vector2 multSize = new Vector2(1, 1);
	float xSize = 1;

	void Start ()
	{
		Debug.Log("Hello World!");
		System.Console.WriteLine(GameObject.Find("BouncePad").GetComponent<Bouncer>().amp);
	}

	void Update ()
	{
		float move = 0;
		if (Keyboard.current.leftArrowKey.isPressed)
			move --;
		if (Keyboard.current.rightArrowKey.isPressed)
			move ++;
		transform.position += new Vector3(move * moveSpeed * Time.deltaTime, 0);
		if (move != 0)
			xSize = Mathf.Sign(move);
		graphicsTrs.SetWorldScale (multSize.SetX(multSize.x * xSize).SetZ(1));
		print(move);
		System.Console.WriteLine("" + Time.time);
		SpriteRenderer spriteRend = gameObject.AddComponent<SpriteRenderer>();
		System.Console.WriteLine(spriteRend);
	}
}
