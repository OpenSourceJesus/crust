using UnityEngine;

public class Ball : MonoBehaviour
{
	void Update ()
	{
		transform.Rotate(Vector3.right * 90 * Time.deltaTime);
	}
}
